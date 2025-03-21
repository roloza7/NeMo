# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from os.path import basename, splitext

import nemo_run as run

from nemo.collections.llm.gpt.data.squad import SquadDataModule
from nemo.collections.llm.recipes.llama31_405b import finetune_recipe, model
from nemo.collections.llm.recipes.precision.mixed_precision import bf16_with_fp8_mixed
from nemo.lightning.run.plugins import NsysPlugin, PerfEnvPlugin

from ..argument_parser import parse_cli_args
from ..utils import (
    args_sanity_check,
    get_user_configs,
    hf_tokenizer,
    import_ckpt_experiment,
    isfile_train_pack_metadata,
    set_exp_logging_configs,
    set_primary_perf_configs,
    slurm_executor,
)

HF_MODEL_URI = "meta-llama/Llama-3.1-405B"

# Set this to True if checkpoint is available at 'NEMO_HOME'. If set to False,
# extra Slurm job will be scheduled. In this case, if checkpoint is available
# at 'NEMO_HOME', fine-tuning job will use this checkpoint, else, it will be
# downloaded from HuggingFace
SKIP_IMPORT = False


def override_recipe_configs(
    args: str,
    num_nodes: int,
    mbs: int,
    gbs: int,
    tp_size: int,
    pp_size: int,
    cp_size: int,
    vp_size: int,
    ep_size: int,
    enable_cuda_graphs: bool,
):
    """
    llama3.1 405b pre-train recipe aimed at achieving best possible performance.

    NOTE: Use fp8 precision training with caution. It might not give desirable results.
    """
    finetuning_scheme = "none" if args.finetuning == "sft" else args.finetuning
    recipe = finetune_recipe(peft_scheme=finetuning_scheme, performance_mode=True)

    recipe = set_primary_perf_configs(
        recipe,
        finetuning_scheme,
        num_nodes,
        args.gpus_per_node,
        mbs,
        gbs,
        args.max_steps,
        tp_size,
        pp_size,
        cp_size,
        vp_size,
        ep_size,
        enable_cuda_graphs=enable_cuda_graphs,
    )
    recipe = set_exp_logging_configs(
        recipe,
        finetuning_scheme,
        "llm",
        "llama3",
        args.tensorboard,
        args.wandb,
        args.wandb_prj_name,
        args.wandb_job_name,
    )

    # data module configs
    recipe.data.tokenizer = hf_tokenizer(HF_MODEL_URI)
    if recipe.data.__fn_or_cls__ == SquadDataModule and not isfile_train_pack_metadata(HF_MODEL_URI, recipe.data):
        # flag is valid only for SquadDataModule
        recipe.data.force_redownload = True

    # compute dtype configs
    if args.compute_dtype.lower() == "fp8":
        recipe.trainer.plugins = bf16_with_fp8_mixed()
        recipe.trainer.plugins.grad_reduce_in_fp32 = False

    return recipe


if __name__ == "__main__":
    args = parse_cli_args().parse_args()
    args_sanity_check(args)

    kwargs = get_user_configs(args.gpu.lower(), args.finetuning, "llama31", "405b", args)
    num_nodes, mbs, gbs, tp_size, pp_size, cp_size, vp_size, ep_size, _, enable_cuda_graphs = kwargs

    recipe = override_recipe_configs(
        args, num_nodes, mbs, gbs, tp_size, pp_size, cp_size, vp_size, ep_size, enable_cuda_graphs
    )

    exp_config = f"{num_nodes}nodes_tp{tp_size}_pp{pp_size}_cp{cp_size}_vp{vp_size}_{mbs}mbs_{gbs}gbs"
    exp_name = f"{args.finetuning}_{splitext(basename(__file__))[0]}_{args.compute_dtype}_{exp_config}"

    executor = slurm_executor(
        args.account,
        args.partition,
        args.log_dir,
        num_nodes,
        args.gpus_per_node,
        args.time_limit,
        args.container_image,
        custom_mounts=args.custom_mounts,
        custom_env_vars={},
        hf_token=args.hf_token,
        nemo_home=args.nemo_home,
        wandb_key=args.wandb_key,
    )

    plugins = [PerfEnvPlugin(enable_vboost=True, nccl_pp_comm_chunksize=2097152 if pp_size > 1 else None)]
    if args.enable_nsys:
        plugins.append(NsysPlugin(start_step=5, end_step=6))

    with run.Experiment(exp_name) as exp:
        if not SKIP_IMPORT:
            assert args.hf_token is not None, "HF token is required for importing checkpoint from HuggingFace"
            exp.add(*import_ckpt_experiment(executor, model(), source=f"hf://{HF_MODEL_URI}"))
        exp.add(
            recipe,
            executor=executor,
            name=exp_name,
            plugins=plugins,
        )

        if not args.dryrun:
            exp.run(sequential=True, detach=True)
        else:
            exp.dryrun()
