from copy import deepcopy
import os
from typing import List

from peft import LoraConfig, get_peft_model
import torch
from transformers import (
    AddedToken,
    DataCollatorForSeq2Seq,
    LlamaForCausalLM,
    LlamaTokenizer,
    TrainingArguments,
)
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import logging

from ..util import PeftTrainer, prepare_model_for_training, save_peft_model, temporary_change_attributes

logger = logging.get_logger("transformers")

def load(base_model="huggyllama/llama-{size}", size="7b", base_class=LlamaForCausalLM, model_kwargs={}):
    base_model = base_model.format(size=size)
    token = lambda s: AddedToken(s, lstrip=False, rstrip=False)
    model = base_class.from_pretrained(base_model,
        pad_token_id=0,
        bos_token_id=1,
        eos_token_id=2,
        torch_dtype=torch.float16,
        **model_kwargs
    )
    tokenizer = LlamaTokenizer.from_pretrained(base_model,
        model_max_length=1200,
        unk_token=token("<unk>"),
        bos_token=token("<s>"),
        eos_token=token("</s>"),
        pad_token=token("<unk>"), # same as unk_token
        sep_token=token("<0x1D>"), # ascii group separator
        mask_token=token("<0x1A>" ), # ascii sup token, only used by clima
        padding_side="right", # Note: only for training, need to change to "left" for batched inference
    )

    return model, tokenizer

def preprocess(examples, tokenizer, train_on_inputs=False, clip_only=False, num_patches=0, min_len=100):
    """Construct model inputs and tokenize them"""
    min_len = min_len + num_patches
    patch_prefix = num_patches * tokenizer.mask_token if num_patches else ""

    if clip_only:
        assert num_patches, "When only using CLIP to process inputs the model needs to be multimodal!"

    def tokenize(texts, add_bos_token=True, add_eos_token=False, add_sep_token=False):
        with temporary_change_attributes(tokenizer, add_bos_token=add_bos_token, add_eos_token=add_eos_token):
            result = tokenizer(texts)

            if add_sep_token:
                for input_ids in result["input_ids"]:
                    input_ids.append(tokenizer.sep_token_id)

            result["labels"] = deepcopy(result["input_ids"])
            result.pop("attention_mask") # won't work with below truncation code and data collator will take care of it anyway

        return result

    def try_truncate(ids, max_len):
        while len(ids) > max_len and not len(ids) <= min_len:
            for idx in reversed(range(len(ids))):
                # make sure to not remove special tokens
                if ids[idx] not in tokenizer.all_special_ids:
                    ids.pop(idx)
                    break
            else:
                break
        return ids

    captions = tokenize([patch_prefix + ("" if clip_only else caption) for caption in examples['caption']], add_sep_token=True)
    codesnippets = tokenize(examples['code'], add_bos_token=False, add_eos_token=True)

    if not train_on_inputs:
        captions["labels"] = [[-100] * len(labels) for labels in captions["labels"]]

    for key, val in codesnippets.items():
        for instruction_ids, code_ids in zip(captions[key], val):
            # try to truncate caption, when len(caption) + len(code) > tokenizer.model_max_length
            try_truncate(instruction_ids, tokenizer.model_max_length - len(code_ids)).extend(code_ids)

    return captions

# https://github.com/tloen/alpaca-lora#official-weights
def train(
    output_dir: str,
    model,
    tokenizer,
    dataset,
    overwrite=False,
    # training hyperparams
    batch_size: int = 128,
    micro_batch_size: int = 1,
    num_epochs: int = 12,
    learning_rate: float = 5e-4,
    gradient_checkpointing = False,
    # lora hyperparams
    lora_r: int = 64,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    lora_target_modules: List[str] = [ # defaults to all linear layers of llama
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        'up_proj',
        'down_proj',
        'gate_proj'
    ],
    full_finetune_modules: List[str] = [
        "embed_tokens",
        "lm_head"
    ],
    # llm hyperparams
    train_on_inputs: bool = False,  # if False, masks out inputs in loss
    group_by_length: bool = False,  # faster when True, but produces an odd training loss curve
):
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    gradient_accumulation_steps = batch_size // micro_batch_size
    if ddp := world_size != 1:
        gradient_accumulation_steps = gradient_accumulation_steps // world_size

    config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        target_modules=lora_target_modules,
        modules_to_save=full_finetune_modules,
        lora_dropout=lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(prepare_model_for_training(
        model=model,
        modules_to_save=full_finetune_modules,
        use_gradient_checkpointing=gradient_checkpointing),
        peft_config=config
   )

    last_checkpoint = None
    if os.path.isdir(output_dir) and not overwrite:
        last_checkpoint = get_last_checkpoint(output_dir)
        if last_checkpoint is None and len(os.listdir(output_dir)) > 0:
            raise ValueError(
                f"Output directory ({output_dir}) already exists and is not empty. "
                "Use `overwrite` to overcome."
            )
        elif last_checkpoint is not None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `output_dir` or add `overwrite` to train from scratch."
            )

    train_data = dataset.map(
        preprocess,
        batched=True,
        remove_columns=dataset.column_names,
        fn_kwargs=dict(  # pyright: ignore
            tokenizer=tokenizer,
            train_on_inputs=train_on_inputs
        )
    )
    logger.info(f"Dataset size before filtering out too long examples: {len(train_data)}")
    train_data = train_data.filter(lambda example: len(example['input_ids']) <= tokenizer.model_max_length)
    logger.info(f"Dataset size after filtering out too long examples: {len(train_data)}")

    trainer = PeftTrainer(
        model=model,
        train_dataset=train_data,
        args=TrainingArguments(
            per_device_train_batch_size=micro_batch_size,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=0.03,
            num_train_epochs=num_epochs,
            learning_rate=learning_rate,
            fp16=True,
            #bf16=True,
            #tf32=True,
            logging_steps=10,
            lr_scheduler_type="cosine",
            optim="adamw_torch",
            save_strategy="epoch",
            output_dir=output_dir,
            save_total_limit=1,
            ddp_find_unused_parameters=False if ddp else None,
            group_by_length=group_by_length,
        ),
        data_collator=DataCollatorForSeq2Seq(
            tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True
        ),
    )

    model.config.use_cache = False
    model = torch.compile(model)

    trainer.train(resume_from_checkpoint=last_checkpoint)
    save_peft_model(model, output_dir) # type: ignore
    trainer.save_state()

    return model, tokenizer
