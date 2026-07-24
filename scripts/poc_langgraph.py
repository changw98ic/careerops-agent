# ruff: noqa: RUF001, RUF002, RUF003

"""PoC: 验证 LangGraph interrupt/resume 语义（回答 plan v0.2 #1 的全部疑问）。

验证点：
1. interrupt(payload) 返回 resume 值；payload 必须 JSON 可序列化（UUID→str，非 dataclass）
2. Command(goto=<node>) 路由到节点名（send / END），不是 interrupt()
3. thread_id 在 config["configurable"]，不在 state
4. 节点返回 dict 更新 state（非原地改）
5. 【关键】resume 时 review_gate 节点是否重跑？（_call_count 计数）
   → 若重跑，interrupt 前副作用（kernel.propose/request_approval）会重复 → 需幂等
6. get_state().next / .values / .tasks

脚本包含断言；任一语义变化都会以非零退出码失败，而不是只打印观察结果。

跑：uv run --no-project --with langgraph==1.2.9 python scripts/poc_langgraph.py
PoC 非项目代码，验证后删除。
"""

from __future__ import annotations

import importlib.metadata
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

print("langgraph version:", importlib.metadata.version("langgraph"))

_call_count = {"review": 0}


class State(TypedDict, total=False):
    items: int
    sent: bool
    decision_value: str


def draft_node(state: State) -> dict:
    print("  [draft_node] set items=3")
    return {"items": 3}  # 节点返回 dict 更新 state（非原地改）


def review_gate(state: State):
    _call_count["review"] += 1
    n = _call_count["review"]
    print(f"  [review_gate] enter #{n}, items={state.get('items')}")
    # interrupt payload 用 JSON 可序列化值（str/int），不用 UUID/dataclass
    decision = interrupt({"approval_id": "approval-xyz", "items": state.get("items", 0)})
    print(f"  [review_gate] resumed, decision={decision!r}")
    # Command.goto 是节点名（send / END），不是 interrupt()
    if decision == "approve":
        return Command(goto="send", update={"decision_value": decision})
    return Command(goto=END, update={"decision_value": decision})


def send_node(state: State) -> dict:
    print(f"  [send_node] execute, items={state.get('items')}")
    return {"sent": True}


def build_graph():
    g = StateGraph(State)
    g.add_node("draft", draft_node)
    g.add_node("review", review_gate)
    g.add_node("send", send_node)
    g.add_edge(START, "draft")
    g.add_edge("draft", "review")
    g.add_edge("send", END)
    # review 出边由 Command(goto) 动态决定，不加固定 edge
    return g.compile(checkpointer=MemorySaver())


def main() -> None:
    graph = build_graph()
    cfg = {"configurable": {"thread_id": "t1"}}  # thread_id 在 config，不在 state

    print("\n=== invoke 1: 应在 review_gate interrupt ===")
    graph.invoke({"items": 0}, cfg)

    st = graph.get_state(cfg)
    assert st.next == ("review",), st.next
    assert st.values["items"] == 3, st.values
    assert len(st.tasks) == 1 and len(st.tasks[0].interrupts) == 1, st.tasks
    assert st.tasks[0].interrupts[0].value == {
        "approval_id": "approval-xyz",
        "items": 3,
    }, st.tasks
    print(f"\n=== get_state: next={st.next}")
    print(f"    values={dict(st.values)}")
    print(f"    tasks={[(t.name, getattr(t, 'interrupts', None)) for t in st.tasks]}")

    print("\n=== invoke 2: Command(resume='approve') ===")
    graph.invoke(Command(resume="approve"), cfg)

    st2 = graph.get_state(cfg)
    assert st2.next == (), st2.next
    assert st2.values["sent"] is True, st2.values
    assert st2.values["decision_value"] == "approve", st2.values
    assert _call_count["review"] == 2, _call_count
    print(f"\n=== final: next={st2.next}, values={dict(st2.values)}")

    print(f"\n=== review_gate 总进入次数: {_call_count['review']} ===")
    print("    ==1 → resume 不重跑节点，interrupt 前副作用不重复（利好）")
    print("    ==2 → resume 重跑节点，interrupt 前副作用会重复（需幂等）")


if __name__ == "__main__":
    main()
