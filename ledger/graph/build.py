from functools import partial

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ledger.contracts.state import InvoiceState
from ledger.graph.nodes import analysts, approve, extract, match, payment_intent, policy, route
from ledger.llm.router import ModelRouter


def pool(url: str) -> ConnectionPool:
    return ConnectionPool(
        url,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )


def postgres_saver(conn: ConnectionPool) -> PostgresSaver:
    saver = PostgresSaver(conn)
    saver.setup()
    return saver


def thread(invoice_id: str) -> dict:
    return {"configurable": {"thread_id": invoice_id}}


def build(checkpointer: BaseCheckpointSaver, router: ModelRouter) -> CompiledStateGraph:
    g = StateGraph(InvoiceState)
    g.add_node("extract", partial(extract.run, router=router))
    g.add_node("match", match.run)
    for name in analysts.ANALYSTS:
        g.add_node(name, partial(analysts.run, name=name, router=router))
        g.add_edge(name, "policy")
    g.add_node("policy", policy.run)
    g.add_node("approve", approve.run)
    g.add_node("payment_intent", payment_intent.run)

    g.add_edge(START, "extract")
    g.add_conditional_edges(
        "extract", lambda s: END if s["status"] == "needs_human" else "match", ["match", END]
    )
    g.add_conditional_edges("match", route.route, [*analysts.ANALYSTS, "policy", END])
    g.add_conditional_edges("policy", policy.next_step, ["approve", "payment_intent", END])
    g.add_conditional_edges("approve", approve.next_step, ["payment_intent", END])
    g.add_edge("payment_intent", END)
    return g.compile(checkpointer=checkpointer)
