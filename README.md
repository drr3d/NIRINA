<p align="center">
  <img src="images/NIRINA_logo.png" width="20%">
</p>

# N.I.R.I.N.A.

> **NaN Intelligent Random Initiator Neuro-Artificial**  
> A lightweight Agentic AI Framework designed to maximize the capabilities of small-parameter local LLMs through structured reasoning, tool orchestration, and stateful execution.

---

## 🧠 What is N.I.R.I.N.A.?

**N.I.R.I.N.A.** is a modular **Agentic AI Framework** designed to turn small local language models (SLMs) into capable, tool-using autonomous agents.

Instead of relying entirely on large cloud-based models, N.I.R.I.N.A. focuses on improving the **system around the model**:

- Structured reasoning
- Stateful execution
- Controlled tool calling
- Execution feedback
- Iterative decision-making
- Modular skill integration

The core philosophy is simple:

> **Don't make the model bigger. Make the system around the model smarter.**

N.I.R.I.N.A. is built on top of **LangGraph** and **LangChain**, providing a graph-driven execution layer where the LLM acts as the reasoning core while external capabilities are provided through modular tools.

---

## 💡 Core Concept — Brain & Sensors(Tools)

N.I.R.I.N.A. follows a **Brain & Sensors Architecture**.

### 🧠 The Brain

The **Brain** is the core agent responsible for the cognitive and execution loop:

- State management
- Reasoning
- Decision making
- Tool selection
- Workflow orchestration
- Error handling
- Execution feedback
- Iterative reasoning

The Brain remains relatively lightweight. External capabilities are delegated to modular Sensors.

### 📡 The Sensors(Tools)

**Sensors** are modular tools that extend what the agent can perceive and do.

A Sensor can represent almost anything:

- Web search
- APIs
- File processing
- Database access
- Security tools
- System utilities
- Custom business logic
- Domain-specific skills

Sensors can be added, removed, or replaced without fundamentally changing the Brain.

This makes N.I.R.I.N.A. **domain-agnostic by design**.

---

## ⚙️ Agent Execution Philosophy

N.I.R.I.N.A. is designed around the following execution principle:

**Observe → Reason → Decide → Act → Observe Result → Reflect → Repeat**

Instead of allowing an SLM to generate a complete solution purely through text generation, N.I.R.I.N.A. gives the model access to **real execution feedback**.

A typical execution flow is:

1. Receive the user request.
2. Analyze the current state.
3. Determine the next action.
4. Select an appropriate tool or Sensor.
5. Execute the tool.
6. Observe the result.
7. Evaluate the result.
8. Continue, correct, or terminate the workflow.

A failed tool execution can therefore become **feedback**, rather than simply a terminal failure.

---

## 🛡️ Hallucination Mitigation

Small Language Models can be highly capable, but they are generally more sensitive to:

- Reasoning drift
- Invalid tool calls
- Incorrect parameters
- Fabricated APIs
- Incorrect assumptions
- Execution failures

N.I.R.I.N.A. does **not** claim to eliminate hallucinations.

Instead, it aims to **reduce the probability and impact of hallucinated actions** by constraining what the agent can actually execute.

The architecture takes conceptual inspiration from research such as **Gorilla LLM** and **Voyager**.

### 🦍 Gorilla — API & Tool Grounding

The The **[Gorilla LLM](https://github.com/ShishirPatil/gorilla)** project demonstrates the importance of grounding LLM-generated actions against available APIs and tools. approach demonstrates the importance of grounding LLM-generated actions against available APIs and tools.

N.I.R.I.N.A. adopts a similar principle:

> **The model should reason over the tools that actually exist, rather than inventing capabilities.**

Structured tool interfaces help constrain:

- Available tools
- Tool parameters
- Expected inputs
- Execution interfaces

This reduces the search space for tool calling and makes invalid actions easier to detect.

### 🧱 Voyager — Execution & Feedback

The **[Voyager](https://github.com/MineDojo/Voyager)** project demonstrates the value of an iterative interaction loop where an agent learns from actual execution rather than relying purely on textual reasoning.
 demonstrates the value of an iterative interaction loop where an agent learns from actual execution rather than relying purely on textual reasoning.

N.I.R.I.N.A. applies a similar philosophy:

**Plan → Execute → Observe → Evaluate → Correct → Execute Again**

A failed tool call can therefore become **feedback** that helps guide the next action.

---

## ⚡ Key Features

### 🧠 Model-Agnostic Optimization

N.I.R.I.N.A. is designed to work across a wide range of local LLMs, from small-parameter models running on limited hardware to larger models deployed on high-performance systems.

The framework focuses on improving agent capabilities through the system surrounding the model rather than depending solely on model size.

N.I.R.I.N.A. can leverage:

- Small local models for resource-efficient deployments
- Larger local models when more capable hardware is available
- Different model architectures and runtimes
- Structured workflows and orchestration
- Tool grounding
- Execution feedback
- Iterative reasoning

The goal is not to enforce a specific model size, but to provide a flexible agent architecture that can **scale with the available hardware and model capabilities**.

### 🔌 Modular Sensor Architecture

Tools are treated as independent modules.

New capabilities can be added without rewriting the entire agent.

### 🔄 Graph-Driven Execution

Built with **LangGraph**, allowing complex stateful workflows to be represented as explicit execution graphs.

### 🧩 Tool-Oriented Reasoning

The agent can reason about available tools and select appropriate capabilities based on the current task.

### 🔁 Iterative Feedback Loop

Tool execution results and errors can feed back into the reasoning process.

### 🔒 Local & Private

Designed to work with locally hosted models, minimizing dependency on external cloud inference.

### 🌐 Domain Agnostic

The same Brain can be equipped with completely different Sensors depending on the mission.

---

## 🚀 Implementations

N.I.R.I.N.A. is designed to be **domain-agnostic**.

The Brain remains largely unchanged while the Sensor layer can be customized for different missions.

### 📄 HR Recruitment Agent

**Status:** `Available`

Example capabilities:

- Resume parsing
- Candidate information extraction
- Candidate evaluation
- Recruitment workflow orchestration
- Interview workflow handling

### 🛡️ Security & Recon Agent

**Status:** `Work in Progress`

Example capabilities:

- Endpoint discovery
- Web reconnaissance
- Deep crawling
- Security-oriented tool orchestration
- Automated reconnaissance workflows

> Tools can be isolated from the core agent environment when required.

---

## 🏗️ Architecture

N.I.R.I.N.A. separates **reasoning** from **capabilities**.

The architecture consists of several conceptual layers:

### Brain

The central agent layer responsible for:

- State
- Reasoning
- Decision making
- Workflow control
- Execution loops

### Sensors

The modular tool layer responsible for providing external capabilities to the Brain.

### Execution Layer

The execution layer connects reasoning with real-world actions through structured tool calls and feedback.

### Model Layer

N.I.R.I.N.A. is designed to work with locally hosted small-parameter models through runtimes such as:

- Ollama
- llama.cpp

This separation allows the same Brain architecture to operate across different domains by changing the available Sensors and models.

---

## 🛠️ Technology Stack

| Component | Technology |
|---|---|
| Agent Framework | LangGraph |
| Tool Integration | LangChain |
| LLM Runtime | Ollama / llama.cpp |
| Local Models | Qwen, Llama, and other SLMs |
| Language | Python 3.11+ |
| UI | Streamlit |
| Architecture Inspiration | Gorilla LLM, Voyager |

---

## 🎯 Design Goals

N.I.R.I.N.A. is built around several core goals:

1. **Make small local models more useful.**
2. **Reduce unnecessary dependence on large cloud models.**
3. **Make tool execution explicit and controllable.**
4. **Use execution feedback to improve reliability.**
5. **Keep agent capabilities modular.**
6. **Make complex agent workflows observable and debuggable.**
7. **Make the framework adaptable to different domains.**

---

## 📌 Project Philosophy

> **The LLM doesn't need to know everything.**  
> **It needs to know how to think, when to act, and which Sensor/Tool to use.**

N.I.R.I.N.A. treats the LLM as the **reasoning core**, while Sensors/Tool provide the capabilities required to interact with the real world.

**Small Model. Structured Brain. Modular Sensors/Tool. Real Execution.**

---

## 📊 Project Status

> 🚧 **N.I.R.I.N.A. is currently under active development.**

The framework architecture is evolving as new agent workflows, tools, models, and execution strategies are tested.

---

## 📜 License

License information will be added as the project approaches public release.