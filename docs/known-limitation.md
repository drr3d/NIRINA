# 🚧 Known Limitations & Future Improvements

RekrutYuk is still actively evolving. While the current architecture performs well for most day-to-day recruitment tasks, several areas are being researched and continuously improved.

This document summarizes the current limitations and possible future directions.

---

# 1. Multi-Intent Request Execution

## Current Behavior

The current Agentic AI workflow performs well when handling one or two related recruitment tasks.

Example:

> Find backend candidates with at least 3 years of experience.

or

> Compare Candidate A and Candidate B.

However, more complex recruitment workflows usually consist of **multiple dependent tasks**, where the result of one step becomes the prerequisite for the next.

For example:

> Find qualified frontend candidates. If none are found, create a frontend job posting. Once candidates become available, compare the best applicants, generate interview questions, and finally schedule interviews.

Although this appears to be a single user request, it is actually a **dependent workflow**, not a collection of independent tasks.

Each task relies on the successful completion of the previous one. For example:
- interview scheduling cannot happen before suitable candidates have been identified
- creating a job posting is only necessary if no qualified candidates are found.

Currently, RekrutYuk attempts to reason through the entire workflow within a single planning session. While this approach works well for simple requests, long dependency chains can eventually exceed the reasoning capability of smaller local LLMs, resulting in unstable planning, recursive tool calls, context saturation, or hitting LangGraph's recursion limit.

---

## Why This Becomes Difficult

Several issues may occur during execution:

- 🧠 **Cognitive Overload**

  Smaller local LLMs eventually lose track of the original objective after many reasoning steps.

- 📚 **Context Saturation**

  Tool outputs continue to accumulate inside the conversation context, making reasoning progressively more difficult.

- 🔁 **Recursive Planning**

  The planner may repeatedly invoke tools, revisit previous reasoning, or eventually hit LangGraph's recursion limit.

- 🔒 **Human Approval Conflict**

  Some tools require human approval (Human-in-the-Loop). Mixing approval-required actions with autonomous reasoning sometimes causes the planner to stall.

---

# Temporary Workaround

One workaround that currently improves success rates is increasing the LangGraph recursion limit.

Example:

```python
graph.invoke(
    inputs,
    config={
        "recursion_limit": 50
    }
)
```

The default recursion limit is intentionally conservative to prevent infinite loops.

Increasing the limit allows the planner to complete more reasoning steps.

In testing, increasing the limit to **50** often allows the agent to finish requests containing approximately **three independent tasks**, although success is still inconsistent.

Therefore this should be considered **a temporary workaround rather than a complete solution**.

---

# Proposed Solution

Rather than asking the LLM to solve every objective inside one long reasoning chain, RekrutYuk will likely move toward a **Plan-and-Execute** architecture using a Task Queue.

Expected workflow:

```
User Request
        │
        ▼
     Planner
        │
        ▼
  Pending Tasks
 ┌───────────────┐
 │ Search CV     │
 │ Compare CV    │
 │ Create Job    │
 │ Interview     │
 └───────────────┘
        │
        ▼
 Execute 1-2 Tasks
        │
        ▼
 Shared Context
        │
        ▼
 Remaining Tasks?
        │
       Yes
        │
        ▼
Continue or Human Approval
```

Instead of processing every objective simultaneously, the planner would:

1. Break the request into smaller executable tasks.
2. Execute only one or two tasks at a time.
3. Store intermediate results inside `shared_context`.
4. Continue with the remaining tasks after the current batch completes.
5. Interrupt when human approval is required.

---

## Proposed State

```python
pending_tasks: list

completed_tasks: list

shared_context: dict
```

---

## Expected Benefits

- Better stability on smaller local LLMs
- Reduced context overload
- Lower chance of recursion limit errors
- Easier Human-in-the-Loop integration
- More predictable multi-step execution
- Better scalability for future workflows

---

# References

This limitation is not unique to RekrutYuk.

Several Agentic AI frameworks recommend decomposing large objectives into smaller planning and execution steps.

- LangChain — Plan-and-Execute Agents
  https://blog.langchain.dev/planning-agents/

- LangGraph Documentation
  https://docs.langchain.com/oss/python/langgraph/

---

# Current Status

🟡 Researching

The current implementation is stable for most recruitment workflows.

The Task Queue architecture described above is currently under investigation and may become the default execution strategy in future releases.

> 💡 This document intentionally records design decisions, current limitations, and ongoing research.
> 
> Rather than hiding unfinished ideas, I prefer documenting them so the evolution of RekrutYuk remains transparent to contributors and future maintainers.