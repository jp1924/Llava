import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
from datasets import Dataset, concatenate_datasets, load_dataset
from setproctitle import setproctitle
from trl.trainer.utils import DataCollatorForCompletionOnlyLM

from transformers import (
    HfArgumentParser,
    LlavaForConditionalGeneration,
    LlavaProcessor,
    Trainer,
    TrainingArguments,
    set_seed,
)
from transformers import logging as hf_logging
from transformers.trainer_pt_utils import get_model_param_count
from transformers.utils import is_liger_kernel_available


hf_logging.set_verbosity_info()
logger = hf_logging.get_logger("transformers")
os.environ["TOKENIZERS_PARALLELISM"] = "false"


@dataclass
class LlavaInsturctionArguments(TrainingArguments):
    # data
    dataset_repo_ls: List[str] = field(
        default=None,
        metadata={"help": "The name of the dataset to use (via the datasets library)."},
    )

    preprocessing_num_workers: int = field(
        default=4,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    preprocessing_batch_size: int = field(
        default=1000,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    preprocessing_batched: bool = field(
        default=True,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )

    train_dataset_prefix: List[str] = field(
        default="train",
        metadata={"help": "A prefix required to distinguish splits in the data loaded by load_dataset."},
    )
    valid_dataset_prefix: List[str] = field(
        default="validation",
        metadata={"help": "A prefix required to distinguish splits in the data loaded by load_dataset."},
    )
    test_dataset_prefix: List[str] = field(
        default="eval_other",
        metadata={"help": "A prefix required to distinguish splits in the data loaded by load_dataset."},
    )
    data_truncate_map: Optional[Union[dict, str]] = field(
        default=None,
        metadata={"help": "A map to truncate part of the data. {‘repo_name’: {‘train’: 3000, ‘validation’: 1500}}."},
    )

    cache_file_name: Optional[str] = field(
        default=None,
        metadata={"help": "Path to cached file name"},
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )

    # model
    model_name_or_path: str = field(
        default=None,
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models."},
    )

    def __post_init__(self):
        super().__post_init__()
        self.data_truncate_map = json.loads(self.data_truncate_map) if self.data_truncate_map else None


class DataCollatorForImageCompletion(DataCollatorForCompletionOnlyLM):
    def __init__(self, image_processor, **kwargs):
        super().__init__(**kwargs)
        self.image_processor = image_processor

    def torch_call(self, examples: List[Union[List[int], Any, Dict[str, Any]]]) -> Dict[str, Any]:
        input_ids = [{"input_ids": example["input_ids"]} for example in examples]
        pixel_values = [example["pixel_values"] for example in examples if example["pixel_values"] is not None]

        batch = super().torch_call(input_ids)

        if pixel_values:
            batch["pixel_values"] = torch.stack(pixel_values)

        return batch


def main(train_args: LlavaInsturctionArguments) -> None:
    def preprocessor(example: Dict[str, Union[List[Any], List[List[Any]]]]) -> Dict[str, List[Any]]:
        if "conversations" in example:
            conversations_ls = example["conversations"]
            conversations_ls = conversations_ls if isinstance(conversations_ls, list) else [conversations_ls]
            for idx, conversations in enumerate(conversations_ls):
                try:
                    conversations_ls[idx] = [
                        {
                            "role": chat["role"],
                            "content": json.loads(chat["content"])
                            if re.search(r"\[\{\"type\"\:", chat["content"])
                            else chat["content"],
                        }
                        for chat in conversations
                    ]
                except BaseException as e:  # noqa: F841
                    continue

        try:
            image_ls = example["image"] if "image" in example else [None] * len(conversations_ls)
            image_ls = image_ls if isinstance(image_ls, list) else [image_ls]
        except BaseException as e:  # noqa: F841
            # logger.info(f"image load시 애러 발생: {e}")
            return {
                "pixel_values": [],
                "input_ids": [],
                train_args.length_column_name: [],
            }

        pixel_value_ls = list()
        input_id_ls = list()
        length_ls = list()
        for image, conversation in zip(image_ls, conversations_ls):
            idx = 0
            while conversation[idx : idx + 2]:
                text = processor.tokenizer.apply_chat_template(
                    conversation[: idx + 2], img_token=img_token, tokenize=False
                )
                if image:
                    outputs = processor(
                        images=image,
                        text=text,
                        return_tensors="np",
                    )
                else:
                    outputs = processor.tokenizer(text, return_tensors="np")

                pixel_values, input_ids = outputs["pixel_values"][0] if image else None, outputs["input_ids"][0]

                if image and (image_token_index not in input_ids):
                    break
                elif (image is None) and (image_token_index in input_ids):
                    break
                # elif image and ((image_token_index == input_ids).sum() // 256) != 1:
                elif image and (image_token_index == input_ids).sum() != 1:
                    print(input_ids.tolist())
                    break

                pixel_value_ls.append(pixel_values)
                input_id_ls.append(input_ids)
                length_ls.append(input_ids.shape[0])
                idx += 2

        # 2048 - 256, 257은 이미지, 근데 값이 애매하게 나와서 1700으로 함.
        length_flag = [length <= 1700 for length in length_ls]
        pixel_value_ls = [pixel_value_ls[idx] for idx, flag in enumerate(length_flag) if flag]
        input_id_ls = [input_id_ls[idx] for idx, flag in enumerate(length_flag) if flag]
        length_ls = [length_ls[idx] for idx, flag in enumerate(length_flag) if flag]

        return {
            "pixel_values": pixel_value_ls,
            "input_ids": input_id_ls,
            train_args.length_column_name: length_ls,
        }

    def prepare_datasets() -> Tuple[Optional[Dataset], Optional[Dataset], Optional[Dataset]]:
        train_dataset_ls = valid_dataset_ls = test_dataset_ls = list()
        for repo_name in train_args.dataset_repo_ls:
            logger.info(f"load-{repo_name}")
            datasets = load_dataset(repo_name)

            if repo_name in train_args.data_truncate_map:
                for data_type in train_args.data_truncate_map[repo_name]:
                    truncate_size = train_args.data_truncate_map[repo_name][data_type]
                    data = datasets[data_type].shuffle()
                    if len(data) <= truncate_size:
                        continue

                    datasets[data_type] = data.select(range(truncate_size))

            if train_args.cache_file_name:
                get_cache_path: str = lambda x: os.path.join(  # noqa: E731
                    train_args.cache_dir,
                    f"{name}-{x}_{train_args.cache_file_name}",
                )
                name = repo_name.split("/")[-1]
                train_args.cache_file_name = {x: get_cache_path(x) for x in datasets}

            # DatasetsDict이라서 이런식으로 해줘야 함.
            with train_args.main_process_first(desc="data preprocess"):
                datasets = datasets.map(
                    preprocessor,
                    num_proc=train_args.preprocessing_num_workers,
                    load_from_cache_file=True,
                    batched=train_args.preprocessing_batched,
                    cache_file_names=train_args.cache_file_name,
                    batch_size=train_args.preprocessing_batch_size,
                    remove_columns=set(sum(datasets.column_names.values(), [])),
                    desc=f"preprocess-{repo_name}",
                )
                datasets.set_format("pt")
            for dataset_key in datasets:
                if dataset_key in train_args.train_dataset_prefix and train_args.do_train:
                    train_dataset_ls.append(datasets[dataset_key])
                if dataset_key in train_args.valid_dataset_prefix and train_args.do_eval:
                    valid_dataset_ls.append(datasets[dataset_key])
                if dataset_key in train_args.test_dataset_prefix and train_args.do_predict:
                    test_dataset_ls.append(datasets[dataset_key])

        train_dataset = None
        if train_dataset_ls:
            train_dataset = concatenate_datasets(train_dataset_ls)
            if train_args.local_rank <= 0:
                logger.info(f"train_dataset:\n{train_dataset}")

        valid_dataset = None
        if valid_dataset_ls:
            valid_dataset = concatenate_datasets(valid_dataset_ls)
            if train_args.local_rank <= 0:
                logger.info(f"valid_dataset:\n{valid_dataset}")

        test_dataset = None
        if test_dataset_ls:
            test_dataset = concatenate_datasets(test_dataset_ls)
            if train_args.local_rank <= 0:
                logger.info(f"test_dataset:\n{test_dataset}")

        return (train_dataset, valid_dataset, test_dataset)

    # load model
    model_name_or_path = train_args.resume_from_checkpoint or train_args.model_name_or_path or ""
    model = LlavaForConditionalGeneration.from_pretrained(model_name_or_path)

    img_token = "<|image|>"
    processor = LlavaProcessor.from_pretrained(model_name_or_path, image_token=img_token)
    image_token_index = processor.tokenizer.convert_ids_to_tokens(img_token)

    logger.info(f"before_alive_param: {get_model_param_count(model, trainable_only=True)}")

    for name, parameter in model.named_parameters():
        name = name.split(".")[0]
        if name in ["multi_modal_projector"]:
            parameter.requires_grad = False

    logger.info(f"after_alive_param: {get_model_param_count(model, trainable_only=True)}")

    if is_liger_kernel_available():
        logger.info("now you use liger kernel!")
        from liger_kernel.transformers import apply_liger_kernel_to_llama
        from liger_kernel.triton import apply_liger_triton_cache_manager

        apply_liger_kernel_to_llama()
        apply_liger_triton_cache_manager()

    if train_args.torch_compile:
        model = torch.compile(
            model,
            backend=train_args.torch_compile_backend,
            mode=train_args.torch_compile_mode,
            fullgraph=True,
        )

    # load datasets
    train_dataset, valid_dataset, test_dataset = prepare_datasets()

    # load collator
    response_template = processor.tokenizer.encode("\n\n### Assistant:\n", add_special_tokens=False)[3:]
    collator = DataCollatorForImageCompletion(
        tokenizer=processor.tokenizer,
        image_processor=processor.image_processor,
        response_template=response_template,
    )
    # load trainer
    trainer = Trainer(
        model=model,
        args=train_args,
        tokenizer=processor,
        data_collator=collator,
        train_dataset=train_dataset,
        eval_dataset=valid_dataset,
    )
    if train_args.do_train and train_dataset:
        train(trainer)

    if train_args.do_eval and valid_dataset:
        valid(trainer)

    if train_args.do_predict and test_dataset:
        logger.info("do_predict 코드는 아직 작성 중")


def train(trainer: Trainer) -> None:
    train_args: LlavaInsturctionArguments = trainer.args
    trainer.train(resume_from_checkpoint=train_args.resume_from_checkpoint)

    save_dir = os.path.join(train_args.output_dir, "last_model")
    trainer.save_model(save_dir)


@torch.no_grad()
def valid(trainer: Trainer, valid_datasets: Optional[Union[Dataset, Dict[str, Dataset]]] = None) -> None:
    valid_datasets = valid_datasets if valid_datasets else trainer.eval_dataset
    trainer.evaluate(valid_datasets)


if "__main__" in __name__:
    parser = HfArgumentParser([LlavaInsturctionArguments])
    train_args, remain_args = parser.parse_args_into_dataclasses(return_remaining_strings=True)

    if remain_args:
        logger.info(f"remain_args: {remain_args}")

    if train_args.seed is not None:
        set_seed(train_args.seed)

    if train_args.run_name is not None:
        setproctitle(train_args.run_name)

    main(train_args)