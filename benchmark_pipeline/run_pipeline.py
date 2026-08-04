"""One-command train + evaluate pipeline for all routers on all xRouteBench datasets.

Examples:
    # Everything local (13 trainable/baseline routers x 8 datasets, no API calls)
    python run_pipeline.py --datasets all --routers local

    # A specific pair
    python run_pipeline.py --datasets memory_locomo --routers knnrouter,graphrouter

    # Cost-aware (GraphRouter-style composite reward) training
    python run_pipeline.py --datasets all --routers local --alpha 0.6 --beta 0.4

    # Include API-calling routers (needs API keys, spends money)
    python run_pipeline.py --datasets video --routers all --include-api-routers

Results land in benchmark_pipeline/results/<dataset>_<router>_a<alpha>_b<beta>.json
(existing results are skipped, so the sweep is resumable). Aggregate with:
    python aggregate_results.py
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

DATA_DIR = os.path.join(HERE, "data")
RESULTS_DIR = os.path.join(HERE, "results")
SAVED_MODELS_DIR = os.path.join(HERE, "saved_models")
POOL_DIR = os.path.join(DATA_DIR, "llm_candidates")

DATASETS = [
    "llmrouter_generic", "memory_locomo", "memory_longmemeval", "timeseries",
    "video", "multimodal_geometry3k", "multimodal_mathvista", "personalized",
]

# ── Router categories ────────────────────────────────────────────────────
EMBED_ROUTERS = ["knnrouter", "svmrouter", "mlprouter", "mfrouter",
                 "elorouter", "graphrouter", "routerdc", "hybrid_llm"]
BASELINE_ROUTERS = ["largest_llm", "smallest_llm"]
GENERIC_ROUTERS = ["gmtrouter", "personalizedrouter"]   # trained via registry, routed via route_single
HEAVY_ROUTERS = ["causallm_router"]                      # LoRA finetune + vLLM, 2-stage subprocess
API_ROUTERS = ["knnmultiroundrouter", "llmmultiroundrouter", "router_r1", "automix"]

LOCAL_ROUTERS = EMBED_ROUTERS + BASELINE_ROUTERS + GENERIC_ROUTERS + HEAVY_ROUTERS
ALL_ROUTERS = LOCAL_ROUTERS + API_ROUTERS

with open(os.path.join(HERE, "configs", "hparams.yaml")) as _f:
    ROUTER_HPARAMS = yaml.safe_load(_f)


# ── Reward preprocessing (GraphRouter-style) ─────────────────────────────
def load_pricing():
    with open(os.path.join(POOL_DIR, "default_llm.json")) as f:
        llm_data = json.load(f)
    return {name: (info["input_price"], info["output_price"]) for name, info in llm_data.items()}


def row_costs(df, pricing):
    ip = df["model_name"].map(lambda m: pricing.get(m, (0.0, 0.0))[0])
    op = df["model_name"].map(lambda m: pricing.get(m, (0.0, 0.0))[1])
    return (df.get("input_tokens", 0) * ip + df.get("output_tokens", 0) * op) / 1e6


def preprocess_training_data(train_path, pricing, alpha, beta):
    """Replace `performance` with alpha*norm(perf) - beta*norm(cost); return temp path."""
    df = pd.read_json(train_path, lines=True)
    costs = row_costs(df, pricing).values.astype(float)
    perf = df["performance"].values.astype(float)
    norm_p = (perf - perf.min()) / (perf.max() - perf.min() + 1e-12)
    norm_c = (costs - costs.min()) / (costs.max() - costs.min() + 1e-12)
    df["performance"] = alpha * norm_p - beta * norm_c
    fd, tmp = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    df.to_json(tmp, orient="records", lines=True)
    return tmp


# ── Config builder ───────────────────────────────────────────────────────
def build_config(router, dataset, alpha, beta, train_path=None):
    d = os.path.join(DATA_DIR, dataset)
    tag = f"{dataset}_a{alpha}_b{beta}"
    ext = ".pth" if router == "routerdc" else (".pt" if router == "graphrouter" else ".pkl")
    # Trainers resolve save_model_path relative to the llmrouter package dir,
    # so "saved_models/..." lands at <repo>/llmrouter/saved_models/... ; the
    # load path is resolved from the repo root and must carry the prefix.
    save_rel = os.path.join("saved_models", router, f"{router}_{tag}{ext}")
    load_rel = os.path.join("llmrouter", save_rel)
    cfg = {
        "data_path": {
            "routing_data_train": train_path or os.path.join(d, "routing_data_train.jsonl"),
            "routing_data_test": os.path.join(d, "routing_data_test.jsonl"),
            "query_embedding_data": os.path.join(d, "query_embeddings_train.pt"),
            "llm_data": os.path.join(POOL_DIR, "default_llm.json"),
            "llm_embedding_data": os.path.join(POOL_DIR, "default_llm_embeddings.json"),
        },
        "model_path": {
            "ini_model_path": "",
            "save_model_path": save_rel,
            "load_model_path": load_rel,
        },
        "metric": {"weights": {"performance": alpha, "cost": beta, "llm_judge": 0}},
        "hparam": dict(ROUTER_HPARAMS.get(router, {})),
    }
    if router == "routerdc":
        cfg["model_path"]["backbone_model"] = "microsoft/mdeberta-v3-base"
        cfg["model_path"]["load_model_path"] = save_rel
    elif router == "hybrid_llm":
        cfg["router_mode"] = "deterministic"
        cfg["router_tau"] = 0.1
        cfg["router_threshold"] = 0.5
    elif router == "causallm_router":
        cfg["model_path"]["save_model_path"] = os.path.join(
            "saved_models", "causallm_router", f"causallm_{tag}")
        cfg["model_path"]["load_model_path"] = os.path.join(
            "llmrouter", "saved_models", "causallm_router", f"causallm_{tag}", "merged")
    elif router in API_ROUTERS:
        cfg["api_endpoint"] = os.environ.get("LLM_API_ENDPOINT", "https://api.together.xyz/v1")
        cfg["api_key"] = os.environ.get("LLM_API_KEY")
        if router == "router_r1":
            cfg["router_r1_api_base"] = os.environ.get("ROUTER_R1_API_BASE")
            cfg["router_r1_api_key"] = os.environ.get("ROUTER_R1_API_KEY", "EMPTY")
    return cfg


def write_yaml(cfg):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(cfg, f)
    return path


# ── Train ────────────────────────────────────────────────────────────────
def train_router(router, cfg, yaml_path, device):
    from llmrouter.cli.router_train import ROUTER_TRAINER_REGISTRY
    if router in BASELINE_ROUTERS:
        from llmrouter.models import LargestLLM, SmallestLLM
        cls = LargestLLM if router == "largest_llm" else SmallestLLM
        return cls(yaml_path)
    router_cls, trainer_cls = ROUTER_TRAINER_REGISTRY[router]
    r = router_cls(yaml_path)
    if router == "routerdc":
        r.model.float()
    trainer = trainer_cls(router=r, device=device)
    trainer.train()
    return r


# ── Route test queries ───────────────────────────────────────────────────
def route_test_queries(router, r, cfg, dataset, device):
    import torch
    d = os.path.join(DATA_DIR, dataset)
    test_df = pd.read_json(cfg["data_path"]["routing_data_test"], lines=True)
    uq = test_df.groupby("query", sort=False).first().reset_index()

    if router in BASELINE_ROUTERS:
        best = r.route_single({"query": "probe"})["model_name"]
        return [{"query": q, "routed_model": best} for q in uq["query"]]

    if router == "elorouter":
        from llmrouter.utils import load_model
        elo = load_model(os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"]))
        best = elo.index[0]
        return [{"query": q, "routed_model": best} for q in uq["query"]]

    emb = None
    emb_path = os.path.join(d, "query_embeddings_test.pt")
    if os.path.exists(emb_path):
        emb = torch.load(emb_path, map_location="cpu")

    if router in ("knnrouter", "svmrouter"):
        from llmrouter.utils import load_model
        m = load_model(os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"]))
        out = []
        for _, row in uq.iterrows():
            e = emb[int(row["embedding_id"])].numpy().reshape(1, -1)
            out.append({"query": row["query"], "routed_model": m.predict(e)[0]})
        return out

    if router == "mlprouter":
        from llmrouter.models.mlprouter.router import MLPClassifierNN
        from llmrouter.utils import load_model
        sd = load_model(os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"]))
        net = MLPClassifierNN(input_dim=r.input_dim,
                              hidden_layer_sizes=cfg["hparam"]["hidden_layer_sizes"],
                              num_classes=r.num_classes,
                              activation=cfg["hparam"]["activation"])
        net.load_state_dict(sd)
        net.eval()
        out = []
        for _, row in uq.iterrows():
            e = emb[int(row["embedding_id"])].float().unsqueeze(0)
            with torch.no_grad():
                idx = net.predict(e).item()
            out.append({"query": row["query"], "routed_model": r.idx_to_model[idx]})
        return out

    if router == "mfrouter":
        from llmrouter.models.mfrouter.router import BilinearMF
        from llmrouter.utils import load_model
        sd = load_model(os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"]))
        mf = BilinearMF(cfg["hparam"]["latent_dim"], len(r.model_to_idx), cfg["hparam"]["text_dim"])
        mf.load_state_dict(sd)
        mf.eval()
        out = []
        for _, row in uq.iterrows():
            q = mf.project_text(emb[int(row["embedding_id"])].float())
            idx = torch.argmax(mf.score_all(q)).item()
            out.append({"query": row["query"], "routed_model": r.idx_to_model[idx]})
        return out

    if router == "graphrouter":
        load_path = os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"])
        if os.path.exists(load_path):
            r.gnn_predictor.model.load_state_dict(torch.load(load_path, map_location="cpu"))
        test_emb = np.array([emb[int(row["embedding_id"])].numpy() for _, row in uq.iterrows()])
        n_test = len(test_emb)
        all_emb = np.vstack([r.query_embedding_list, test_emb])
        all_perf = np.concatenate([r.performance_list, np.zeros(n_test * r.num_llms)])
        n_q = len(all_emb)
        org = [q for q in range(n_q) for _ in range(r.num_llms)]
        des = list(range(r.num_llms)) * n_q
        n_e = n_q * r.num_llms
        n_train = r.num_queries_train
        m_train = torch.zeros(n_e); m_train[:n_train * r.num_llms] = 1
        m_test = torch.zeros(n_e); m_test[n_train * r.num_llms:] = 1
        data = r.form_data.formulation(
            query_feature=all_emb, llm_feature=r.llm_embedding,
            org_node=org, des_node=des, edge_feature=all_perf,
            label=all_perf.reshape(-1, 1), edge_mask=m_test, train_mask=m_train,
            valide_mask=torch.zeros(n_e), test_mask=m_test)
        pred = r.gnn_predictor.predict(data)[-n_test:]
        return [{"query": row["query"], "routed_model": r.model_names[pred[i].item()]}
                for i, (_, row) in enumerate(uq.iterrows())]

    if router == "routerdc":
        save_dir = os.path.join(REPO_ROOT, os.path.dirname(cfg["model_path"]["save_model_path"]))
        ckpt = next((p for p in
                     (os.path.join(save_dir, "best_model.pth"),
                      os.path.join(save_dir, "best_training_model.pth"),
                      os.path.join(REPO_ROOT, cfg["model_path"]["save_model_path"]))
                     if os.path.exists(p)), None)
        if ckpt:
            r.model.load_state_dict(torch.load(ckpt, map_location=device))
        r.model = r.model.to(device).eval()
        names = r.test_dataset.router_node
        out = []
        with torch.no_grad():
            for _, row in uq.iterrows():
                toks = r.tokenizer(row["query"], max_length=512, padding="max_length",
                                   truncation=True, return_attention_mask=True,
                                   add_special_tokens=True, return_tensors="pt").to(device)
                res = r.route({"input_ids": toks["input_ids"],
                               "attention_mask": toks["attention_mask"],
                               "temperature": 1.0})
                out.append({"query": row["query"],
                            "routed_model": names[res["predictions"][0].item()]})
        return out

    if router == "hybrid_llm":
        from llmrouter.utils import load_model
        m = load_model(os.path.join(REPO_ROOT, cfg["model_path"]["load_model_path"]))
        out = []
        for _, row in uq.iterrows():
            e = emb[int(row["embedding_id"])].numpy().reshape(1, -1)
            score = max(0.0, min(1.0, float(m.predict(e)[0])))
            chosen = r.small_model_name if score >= r.router_threshold else r.large_model_name
            out.append({"query": row["query"], "routed_model": chosen})
        return out

    # generic fallback (gmtrouter, personalizedrouter, api routers with route_single)
    out = []
    for _, row in uq.iterrows():
        res = r.route_single({"query": row["query"]})
        model = res.get("model_name") or res.get("routed_model")
        out.append({"query": row["query"], "routed_model": model})
    return out


# ── Evaluate by replaying recorded outcomes ──────────────────────────────
def evaluate(routing_results, test_path, pricing, alpha, beta):
    test_df = pd.read_json(test_path, lines=True)
    rows = []
    for rr in routing_results:
        m = test_df[(test_df["query"] == rr["query"]) &
                    (test_df["model_name"] == rr["routed_model"])]
        if len(m) > 0:
            row = m.iloc[0]
            ip, op = pricing.get(rr["routed_model"], (0.0, 0.0))
            price = (row.get("input_tokens", 0) * ip + row.get("output_tokens", 0) * op) / 1e6
            rows.append({"routed_model": rr["routed_model"], "performance": row["performance"],
                         "input_tokens": row.get("input_tokens", 0),
                         "output_tokens": row.get("output_tokens", 0), "price_cost": price})
    df = pd.DataFrame(rows).dropna(subset=["performance"])
    if not len(df):
        return None
    perfs, costs = df["performance"].values, df["price_cost"].values
    norm_p = (perfs - perfs.min()) / (perfs.max() - perfs.min() + 1e-12)
    norm_c = (costs - costs.min()) / (costs.max() - costs.min() + 1e-12)
    dist = df["routed_model"].value_counts()
    return {
        "avg_performance": round(float(df["performance"].mean()), 4),
        "avg_input_tokens": round(float(df["input_tokens"].mean()), 1),
        "avg_output_tokens": round(float(df["output_tokens"].mean()), 1),
        "avg_token_cost": round(float((df["input_tokens"] + df["output_tokens"]).mean()), 1),
        "avg_price_cost": round(float(df["price_cost"].mean()), 8),
        "avg_reward": round(float((alpha * norm_p - beta * norm_c).mean()), 4),
        "num_queries": int(len(df)),
        "routing_distribution": {m: int(c) for m, c in dist.items()},
    }


# ── One job ──────────────────────────────────────────────────────────────
def result_path(dataset, router, alpha, beta):
    return os.path.join(RESULTS_DIR, f"{dataset}_{router}_a{alpha}_b{beta}.json")


def run_job(router, dataset, alpha, beta, device):
    out_path = result_path(dataset, router, alpha, beta)
    if os.path.exists(out_path):
        print(f"[skip] {dataset} x {router} (result exists)")
        return True

    pricing = load_pricing()
    train_path = os.path.join(DATA_DIR, dataset, "routing_data_train.jsonl")
    tmp_train = None
    if not (alpha == 1.0 and beta == 0.0) and router not in BASELINE_ROUTERS:
        tmp_train = preprocess_training_data(train_path, pricing, alpha, beta)

    cfg = build_config(router, dataset, alpha, beta, train_path=tmp_train)
    yaml_path = write_yaml(cfg)
    try:
        if router == "causallm_router":
            ok = run_causallm_stages(dataset, alpha, beta, yaml_path, device)
            if not ok:
                return False
            return os.path.exists(out_path)

        print(f"[train] {dataset} x {router} (alpha={alpha}, beta={beta})")
        r = train_router(router, cfg, yaml_path, device)
        print(f"[route] {dataset} x {router}")
        routing = route_test_queries(router, r, cfg, dataset, device)
        metrics = evaluate(routing, cfg["data_path"]["routing_data_test"], pricing, alpha, beta)
        if metrics is None:
            print(f"[fail]  {dataset} x {router}: no valid replay rows")
            return False
        summary = {"router": router, "dataset": dataset, "alpha": alpha, "beta": beta, **metrics}
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"[done]  {dataset} x {router}: perf={metrics['avg_performance']} "
              f"reward={metrics['avg_reward']} -> {out_path}")
        return True
    except Exception as e:
        print(f"[fail]  {dataset} x {router}: {type(e).__name__}: {e}")
        return False
    finally:
        os.unlink(yaml_path)
        if tmp_train and os.path.exists(tmp_train):
            os.unlink(tmp_train)


def run_causallm_stages(dataset, alpha, beta, yaml_path, device):
    """Train and vLLM-infer in separate processes (a single process OOMs one GPU)."""
    env = dict(os.environ)
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    base = [sys.executable, os.path.join(HERE, "_causallm_stage.py"),
            "--dataset", dataset, "--alpha", str(alpha), "--beta", str(beta),
            "--yaml", yaml_path, "--device", device]
    merged = os.path.join(REPO_ROOT, "llmrouter", "saved_models",
                          "causallm_router", f"causallm_{dataset}_a{alpha}_b{beta}", "merged")
    if not os.path.isdir(merged):
        print(f"[train] {dataset} x causallm_router (subprocess, LoRA)")
        if subprocess.run(base + ["--stage", "train"], env=env).returncode != 0:
            print("[fail]  causallm train stage")
            return False
    print(f"[route] {dataset} x causallm_router (subprocess, vLLM)")
    if subprocess.run(base + ["--stage", "infer"], env=env).returncode != 0:
        print("[fail]  causallm infer stage")
        return False
    return True


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datasets", default="all",
                        help="'all' or comma-separated names: " + ",".join(DATASETS))
    parser.add_argument("--routers", default="local",
                        help="'all', 'local', or comma-separated names: " + ",".join(ALL_ROUTERS))
    parser.add_argument("--alpha", type=float, default=1.0, help="performance weight")
    parser.add_argument("--beta", type=float, default=0.0, help="cost weight")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--include-api-routers", action="store_true",
                        help="also run routers that make live LLM API calls")
    args = parser.parse_args()

    datasets = DATASETS if args.datasets == "all" else args.datasets.split(",")
    if args.routers == "all":
        routers = ALL_ROUTERS if args.include_api_routers else LOCAL_ROUTERS
    elif args.routers == "local":
        routers = LOCAL_ROUTERS
    else:
        routers = args.routers.split(",")

    bad = [d for d in datasets if d not in DATASETS] + [r for r in routers if r not in ALL_ROUTERS]
    if bad:
        parser.error(f"unknown names: {bad}")

    api_requested = [r for r in routers if r in API_ROUTERS]
    if api_requested and not args.include_api_routers:
        print(f"note: skipping API routers {api_requested} (pass --include-api-routers to run them)")
        routers = [r for r in routers if r not in API_ROUTERS]
    if api_requested and args.include_api_routers and not os.environ.get("LLM_API_KEY"):
        print("warning: LLM_API_KEY not set; API routers will likely fail")

    missing = [d for d in datasets
               if not os.path.exists(os.path.join(DATA_DIR, d, "routing_data_train.jsonl"))]
    if missing:
        sys.exit(f"data missing for {missing}. Run: python download_data.py && python generate_embeddings.py")

    print(f"Sweep: {len(datasets)} datasets x {len(routers)} routers "
          f"(alpha={args.alpha}, beta={args.beta})\n")
    ok = fail = 0
    for ds in datasets:
        for r in routers:
            if run_job(r, ds, args.alpha, args.beta, args.device):
                ok += 1
            else:
                fail += 1
    print(f"\nSweep complete: {ok} succeeded, {fail} failed. "
          f"Aggregate with: python aggregate_results.py")


if __name__ == "__main__":
    main()
