# Firmware Lead — 3-Day Trial Project

**Documents in this pack**

1. This brief — what we're asking you to do.
2. **Architecture Context** — the system, every decision we've made and why, and what is still open.

Between them they should answer everything. If something is missing, treat the gap as part of the problem: decide, write down what you assumed, and keep going.

---

## 1. Why you're here

We are building a modular, hardware-synchronized multi-camera capture system. The firmware is the product. The hardware is deliberately commodity and second-sourceable; essentially all of the hard, defensible work lives in capture, timing, the imaging chain and the control plane.

We do not have a firmware team yet. You would be hired to build and lead one. This trial is therefore not a coding exercise — it is a compressed version of the actual job: take an early-stage system with real constraints and unfinished decisions, form a technical opinion, commit to an architecture, and prove one piece of it works on real silicon.

You have three days and you are working independently. We will review your output at the end rather than sitting with you along the way. That is deliberate. Normally this would be a collaborative process; for the trial, how you sequence and scope your own work under a fixed deadline is a large part of what we want to see.

---

## 2. The system

Read the **Architecture Context** document. The short version:

N synchronized cameras, ~10 today and multiple packs of ~10 later. Every camera is a self-contained recording tile — sensor, media SoC, hardware H.265 encode, its own eMMC — running a segment-rotating ring buffer with event-locked retention. Every frame is hardware-triggered so the N streams are frame-aligned, and every frame carries a trigger counter so a dropped frame cannot silently misalign a camera. A controller orchestrates the fleet over Ethernet and never touches video; retrieval is a request to each node, not a mount. Two hardware tiers — a TI-based standard node and a lower-cost Rockchip-based one — are intended to be drop-in interchangeable behind a single control interface that does not exist yet.

Nothing is built. No custom hardware, no firmware.

---

## 3. What is on the bench

- **TI J722SXH01EVM** — quad Cortex-A53 + 2× Cortex-R5F, 8 GB LPDDR4, 32 GB eMMC, four MIPI CSI-2 camera connectors across two CSI2RX instances, Wave5 hardware video encoder (H.264/H.265, V4L2), gigabit Ethernet with TSN, PCIe slot, USB 3.0. Closest to a gateway-class board in our architecture — capture chain plus the outward-facing I/O — so you can exercise both the recording-node role and the gateway role on it.
- **Raspberry Pi camera modules**
- **A Leopard Imaging camera module**
- Also available if you want them: a Jetson, a Raspberry Pi, and ESP32 devkits. There is no Nordic devkit on site; ESP32 is a supported Zephyr target and is the substitute we have.

Assume nothing about what already works. Finding out is part of the exercise.

---

## 4. Scope boundaries

**Hardware work is out of scope.** You should not pick up a soldering iron, build a harness, or modify a board. **Matthew** is on site and handles anything physical — cabling, adapters, mounting, probing, sourcing. Tell him what you need and he will do it.

What *is* in scope is knowing what you need and asking for it clearly and early. Specifying the hardware you require is a firmware lead's job; executing it is not.

**If our preparation blocks you, that is our fault, not yours.** Missing cable, wrong adapter, part that turns out not to be supported — note it in your log, say what it prevented, and move to something that isn't blocked. Sitting stuck for half a day waiting on us is the wrong answer; so is silently dropping a deliverable without telling us why.

---

## 5. What we're asking for

Three deliverables, weighted roughly equally. A beautiful document with nothing running scores no better than working code with no plan behind it.

### 5.1 Firmware architecture and program plan

The document you would hand to the team you are about to hire. Not length — decisions, stated plainly, with the reasoning attached.

- **Architecture.** Component and responsibility decomposition across the media SoC, the supervisor MCU where one exists, and the orchestrator. What runs where, why, and what crosses the boundaries.
- **The module control interface.** Draft the contract that makes two different node implementations interchangeable: trigger semantics, how a frame binds to a trigger index, command set, health and telemetry, error and degraded-mode behaviour, versioning and compatibility. Write it as something that can be tested, not as prose.
- **Time and data model.** How you establish that camera 7's frame 41 200 and camera 2's frame 41 200 are the same instant. How a dropped frame is detected rather than silently absorbed. How a recording is indexed so "the last 90 seconds" is a cheap query. How event-locked retention behaves against a ring that is constantly reclaiming space.
- **Platform strategy.** Linux distribution and build system for the node, kernel and driver strategy including your upstreaming stance, Zephyr workspace layout, update and rollback, secure boot and identity, and how you avoid being trapped by a vendor SDK.
- **Test strategy.** What you automate, what needs hardware in the loop, and what a CI rig looks like for a system whose defining property is timing across N devices.
- **Program.** Staffing, critical path, what is working at 4 weeks / 3 months / 6 months, and the five risks most likely to hurt us with your mitigations.
- **Your position on our open questions.** §12 of the Architecture Context lists seven things we have not decided. Take a position on the ones that touch your work.

### 5.2 One working vertical slice

Pick the thinnest end-to-end path that proves the hard parts are real, and make it run on the J722S EVM. Our suggested target, which you may argue against:

> A camera streams into the EVM, is encoded to H.265 in hardware, and is written to eMMC as a rotating segment ring with an index; a trigger source drives frame timing; every recorded frame is bound to a trigger index that survives into the stored stream; and a control interface can list the ring, fetch a time range, and lock segments against reclaim.

You will not finish all of that in three days. **We expect you to cut scope, and we care more about how you cut it than about how much you finish.** State what you built, what you stubbed, what you dropped, and why — and leave the dropped parts obviously reachable from what you built.

Do not gold-plate. A narrow path that genuinely runs, with numbers behind it, beats a broad path that ran once on your desk.

### 5.3 Evidence and handover

- A git repository. Readable history, and a README from which a competent colleague could reproduce your result starting from a blank EVM.
- **Measurements, not claims.** Whatever you assert, show the number: sustained bitrate, CPU load, frames captured versus dropped over a run of at least 30 minutes, write throughput, trigger interval jitter, recovery when something is unplugged. Pick what matters and measure it.
- A short decision log — choices, alternatives, reasoning, and the dead ends.
- A summary someone can read in half an hour: what works, what doesn't, what you'd do next and in what order.

---

## 6. About our architecture

Everything in the Architecture Context is our current position, reached without a firmware lead in the room. You are explicitly invited to disagree with any of it — the tiering, the supervisor MCU and where it belongs, the trigger and counter scheme, distributed storage, the silicon choices.

"I would change X, here is what it costs and what it buys" is a strong answer. So is "I looked hard at X and would keep it, for these reasons." Silent agreement is weak, and so is a wholesale rewrite into a stack you happen to know that never engages with why the current shape exists.

The decision log in §11 of that document marks which choices are load-bearing and which are preferences. Push on the preferences freely; if you want to move something marked firm, make the case properly.

---

## 7. Practicalities

- Three working days, self-directed. Assume we are unavailable. Matthew handles hardware; everything else, decide and document.
- Use whatever tools, references and AI assistance you normally use. We care about the outcome and your reasoning, not about you working from memory.
- Nothing on the bench is precious.
- At the end, hand over the repository and the documents. We will review them, then sit down and go through your decisions with you.
