"""The graph worker with ground-truth models, for the crash suite."""

from evals.truth import router_for
from ledger.workers import graph_worker
from world.generator import DEV_SEED, generate

if __name__ == "__main__":
    graph_worker.main(router_for(generate(500, DEV_SEED)))
