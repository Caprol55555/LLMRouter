"""CausalLMRouter stage runner (invoked by run_pipeline.py as a subprocess).

Training fills a GPU with the LoRA-finetuned base model; loading vLLM in the
same process OOMs a single 48GB card. Running the two stages as separate
processes keeps each within one GPU.
"""

import argparse
import json
import os
import sys

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

DATA_DIR = os.path.join(HERE, "data")
RESULTS_DIR = os.path.join(HERE, "results")
POOL_DIR = os.path.join(DATA_DIR, "llm_candidates")


def stage_train(yaml_path):
    hf = os.environ.get("HF_TOKEN", "")
    if hf:
        os.environ["HUGGING_FACE_HUB_TOKEN"] = hf
    from llmrouter.models.causallm_router import CausalLMRouter, CausalLMTrainer
    router = CausalLMRouter(yaml_path)
    trainer = CausalLMTrainer(router=router, device="cuda")
    trainer.train()
    print("[causallm] train done")


def stage_infer(dataset, alpha, beta, yaml_path):
    from vllm import LLM, SamplingParams
    from llmrouter.models.causallm_router import CausalLMRouter

    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)
    router = CausalLMRouter(yaml_path)
    merged = os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"])
    vllm_model = LLM(model=merged, trust_remote_code=True,
                     tensor_parallel_size=1, gpu_memory_utilization=0.9)
    sp = SamplingParams(max_tokens=32, temperature=0.1, top_p=0.95,
                        stop=["\n", "Query:", "Available"])

    test_path = cfg["data_path"]["routing_data_test"]
    test_df = pd.read_json(test_path, lines=True)
    uq = test_df.groupby("query", sort=False).first().reset_index()
    prompts = [router._build_prompt(q) for q in uq["query"]]
    outs = vllm_model.generate(prompts, sp)
    routing = [{"query": row["query"],
                "routed_model": router._parse_llm_name(outs[i].outputs[0].text)}
               for i, (_, row) in enumerate(uq.iterrows())]

    # replay evaluation (same convention as run_pipeline.evaluate)
    from run_pipeline import evaluate, load_pricing, result_path
    metrics = evaluate(routing, test_path, load_pricing(), alpha, beta)
    if metrics is None:
        sys.exit("[causallm] no valid replay rows")
    summary = {"router": "causallm_router", "dataset": dataset,
               "alpha": alpha, "beta": beta, **metrics}
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = result_path(dataset, "causallm_router", alpha, beta)
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[causallm] perf={metrics['avg_performance']} reward={metrics['avg_reward']} -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--alpha", type=float, required=True)
    ap.add_argument("--beta", type=float, required=True)
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--stage", required=True, choices=["train", "infer"])
    args = ap.parse_args()
    if args.stage == "train":
        stage_train(args.yaml)
    else:
        stage_infer(args.dataset, args.alpha, args.beta, args.yaml)
