# Architecture Context

Companion to the trial brief. This is everything we have decided, why we decided it, and what is still open. It is assembled from our design work to date and is deliberately complete, because you should not need to interrupt anyone to understand the system.

Read §11 and §12 last but do read them — the decision log tells you which choices are load-bearing, and the open-questions list tells you what you are allowed to decide yourself.

---

## 1. The product in one paragraph

A rig of N synchronized cameras that records continuously to local storage and offloads afterwards. Every camera exposes on the same trigger instant, so the N streams are frame-aligned and can be used for downstream reconstruction. Each camera is a self-contained recording tile — sensor, media SoC, encoder, its own storage — and a central controller orchestrates them over a network. N is about 10 today, in a single pack. The intent is to scale to several interconnected packs of ~10, and eventually to be effectively unbounded.

---

## 2. Capture requirements

| | Minimum | Standard tier |
|---|---|---|
| Resolution | 1920×1200 native, 1080p window | same |
| Frame rate | 30 fps sustained at full res | 60 fps |
| Shutter | global, color (RGB / Bayer CFA) | same |
| Bit depth | ≥8-bit output, 10-bit at sensor acceptable | same |
| Codec | H.265/HEVC Main, 8-bit 4:2:0 | same |
| Session length | 8 h | 24 h |
| Storage per camera | 32–64 GB eMMC | 128–256 GB eMMC |

H.264 is explicitly excluded. AV1 was considered and rejected for now: no low-power hardware AV1 encoders exist in this device class. Revisit when they do.

Exposure, gain and white balance must all be directly settable and any auto mode must be fully defeatable — consistency across a synced rig matters more than per-camera cleverness.

External hardware trigger is required. Free-run is a nice-to-have.

Sizing we have worked from (H.265, good quality):

- 1080p30 ≈ 5 Mbps ≈ 0.6 MB/s ≈ 18 GB per camera per 8 h
- 1080p60 ≈ 8–10 Mbps ≈ 1.2 MB/s ≈ 100 GB per camera per 24 h
- IPC-class low-bitrate operation is 2–4 Mbps, i.e. 20–40 GB/day, which pulls over 100 Mbit Ethernet in well under an hour per camera

Optics are settled and out of your scope, but they constrain the sensor format so they are recorded here: HFOV ≥ 110°, VFOV ≥ 80°, DFOV ≤ 180° preferred, landscape, M12/S-mount, IR-cut, fixed focus, bare board (we design the enclosure).

---

## 3. Board-level architecture

We use B-numbers internally. Two tiers exist in parallel and are meant to be interchangeable.

| Board | Standard tier | Lite tier | Role |
|---|---|---|---|
| **B0** | Sensor carrier: AR0234CS color GS, IMU, calibration EEPROM, FSIN, MIPI CSI-2 + I²C + SPI over FPC | **B0-lite**: same board concept, lower-cost sensor — spec'd by performance (native ≥1920×1200, GS, color, ~1/2.6″, ~3 µm, documented trigger/slave timing), e.g. SmartSens SC233HGS or functional equivalent | Sensor + optics + per-unit calibration data |
| **B1** | Camera/recording node: TI AM62A3 chip-down, LPDDR4, eMMC, PMIC tree, 2× RGMII/SGMII into the switch plane | **B1-lite**: Rockchip RV1106 (stacked DRAM, integrated 10/100 FE MAC+PHY), eMMC, **plus an nRF54L15 module supervisor** | Capture → ISP → H.265 encode → local ring buffer → network |
| **B2** | Gateway camera: same capture chain plus cellular (M.2 over USB2), 10G NIC over PCIe, WiFi | **B2-lite**: Rockchip RV1126, gigabit RGMII, USB2, SDIO WiFi | A camera that is also the bridge to the outside world |
| **B3** | Orchestration board: nRF54L15 + LAN9668-class TSN switch, 48 V PoE distribution | — | Fleet control, switch fabric, time authority |
| **B4** | nRF54L15-based | — | Role not captured in the material handed to you — treat as out of scope |

None of this is built. The J722S EVM on your bench is closest to a **B2-class** board — it has the capture chain plus the gateway I/O (PCIe for WiFi or an SSD, USB 3.0, gigabit TSN Ethernet), which B1 does not. That is convenient: it lets you exercise both the node role and the gateway role on one board. It is a stand-in, not the committed production part — see §12.

### Why the lite tier has an MCU and the standard tier does not

This is the part people get backwards, so it is worth stating plainly.

**On the standard tier, the TI SoC owns the trigger.** The AM62A3 has proper 1588/TSN hardware timestamping on its Ethernet MACs. It is a gPTP endpoint in its own right, disciplines its clock from the network, and generates or consumes the FSIN edge itself. No supervisor MCU is required, and there isn't one in the B1 design.

**On the lite tier, that capability is missing.** The RV1106's integrated 10/100 MAC is not documented as having a 1588 timestamping unit, vendor-kernel PTP support there is unproven, software-timestamped PTP over Fast Ethernet is tens-to-hundreds of microseconds and load-sensitive, and turning disciplined time into a low-jitter pulse from under Linux is a genuine project that would deliver worse sync than a wire. So B1-lite front-loads an **nRF54L15 supervisor** — the same part as B3 — to restore the capability the TI part has natively:

- FSIN generation or reception with hardware-deterministic edges and edge timestamping (GPIOTE + TIMER + DPPI), with no Linux in the path
- Trigger counting; hands counter + timestamp to the RV1106 for embedding in stream metadata
- The orchestration-link endpoint (RS-485 on the existing cable, or nRF-to-nRF 2.4 GHz time transfer)
- Power sequencing, heartbeat watchdog and power-cycle recovery of the media SoC, update rollback enforcement, secure identity
- Optionally the IMU on SPI, so IMU samples and FSIN edges are timestamped on the same clock — which makes camera↔IMU time alignment free instead of being the usual worst part of a reconstruction pipeline

Two hard boundaries on the supervisor: **it never touches the video path** (no bandwidth, no USB host role — video goes SoC → Ethernet), and it must not grow into a second application platform. Supervisor, timekeeper, and protocol endpoint only.

The point of the exercise is that **the orchestrator cannot tell a B1 from a B1-lite.** Both present the same control interface. Different silicon, different internal means of hitting the trigger, one contract.

---

## 4. Timing and synchronization

The whole product rests on this section.

**Every frame is hardware-triggered.** Sensors run in slave/triggered mode with the exposure start pinned by an external edge on FSIN. Because every frame is re-pinned, oscillator drift between sensors cannot accumulate — which is why a shared reference clock is a refinement rather than a requirement for frame-accurate alignment. (Sharing INCK from one oscillator through a clock buffer removes the ppm offset between crystals and buys nanosecond-class exposure alignment if we ever need it.)

**Within a pack (N ≤ ~10–32, co-located): distribute an edge.** One hardware timer/PWM output → a fanout buffer or a differential line driver (RS-422/485, roughly $0.50 a node) → every sensor's FSIN. Buffer skew is tens of picoseconds; the real error is cable-length mismatch at ~5 ns/m, so lengths get matched. This lands sub-microsecond in practice and trivially satisfies our stated requirement. The trigger must come from a hardware timer peripheral, never a software-toggled GPIO.

**Between packs: distribute time, not an edge.** gPTP over wired Ethernet, each pack controller a gPTP endpoint generating its own local trigger from synced time. Scaling then means adding switch ports. This is why we only need ~5 gPTP nodes for 50 cameras, not 50.

**gPTP requires hardware timestamping at the MAC/PHY, which rules out the transports we would have preferred.** WiFi has no deterministic PHY timestamp and Zephyr's stack does not carry gPTP over it. USB CDC-NCM/ECM is software-emulated with no PHY timestamp either. The connector is separable from the protocol, though — a real timestamping PHY can be broken out to a compact connector, or to Single Pair Ethernet (10BASE-T1L via ADIN1100/ADIN1110 keeps full gPTP at 10 Mbps, enough for sync and control; 100BASE-T1 if we need more). Inter-pack distance is 2–3 m.

**Frames must bind to trigger indices.** Every frame carries a trigger counter in its stream metadata. Whatever generated the trigger logs counter ↔ UTC. This is the mechanism that keeps ten cameras aligned; without it a single dropped frame silently misaligns a camera for the rest of the session and nothing downstream can tell. We consider this the single most important line in the whole system, and it is the thing we would most like to see you take seriously.

Note the useful consequence: with a single trigger source, the clock authority relocates rather than disappearing. Even where per-camera PTP is unavailable, global timestamps survive intact via the counter scheme.

---

## 5. Storage and retrieval

**Storage is distributed and locally owned.** Each node writes its own encoded stream to its own eMMC. Nothing else mounts it — eMMC is point-to-point single-master, so the SoC that writes it owns it. The controller does not multiplex storage and never touches video in real time; it moves triggers and commands, a few kbps of control plane, which is what keeps a small controller viable at all.

This was a deliberate trade. Centralized capture — one aggregator ingesting N streams and writing them to a single device — caps you at that aggregator's input count and bandwidth and forces a heavy host or an FPGA. Distributed capture with per-tile storage is what makes flexible N possible. Centralization happens *between* sessions, not during them: the controller pulls files from each node into a central store afterwards.

**eMMC, not SD, not raw NAND.** SD has no trustworthy wear levelling, a connector that fails, and a counterfeit market. Raw SPI NAND pushes flash management (UBI/UBIFS, bad-block handling, wear levelling) into our firmware, and continuous recording is exactly the workload that punishes that. eMMC's on-die controller does FTL/ECC/wear-levelling for us, industrial grades report health and endurance, and the candidate SoCs boot from it directly so there is no separate boot flash. The delta is a couple of dollars per node.

**Loop recording by segment rotation.** Encode continuously into rotating fixed-size segment files (order 1–10 s each), reclaiming the oldest when the ring fills. Segment rotation rather than a literal circular file is the flash-friendly form — it cooperates with wear levelling and a normal filesystem instead of hammering one region. Keep an index so "the last T seconds" is a cheap query.

**Event lock.** A host command, an external trigger, or a detected event locks recent segments so the ring will not reclaim them. This is dashcam incident protection, and on a synced rig it is a genuinely differentiating feature.

**Retrieval is a request, not a mount.** The controller asks each node's SoC — list segments, give me the last 90 seconds — and the SoC reads its own storage and serves the bytes. The controller is a client of N small file servers, and forwards onward to a PC. Its load is bounded by the upload link, not by camera count.

**Offload.** Ethernet is the backbone. Outward, the gateway can present as USB mass storage, push over WiFi, or use cellular. Full-quality live streaming of all cameras over WiFi is not viable — WiFi is a shared half-duplex medium and ten high-bitrate uplinks contend badly. The model these SoCs are built for is: record full quality locally, stream a low-bitrate proxy substream (say 1080p15 at 3–5 Mbps) for live monitoring. Ten proxies is ~40 Mbps aggregate and is fine.

For a store-and-forward dock, the iron rule is that upload bandwidth must exceed the rate footage is generated, or the cache is not a buffer but a delay line that overflows. Ten cameras at the standard tier generate ~100 Mbps aggregate, so gigabit at the dock has real headroom; a 100 Mbit uplink only works duty-cycled.

---

## 6. The control plane and the drop-in doctrine

There is one interface contract between the orchestrator and any node, and both tiers implement it. We have not written it. It should cover at least:

- Trigger semantics — who generates, who consumes, arm/disarm, rate changes, what happens on loss
- The frame ↔ trigger-index binding and where the counter lives in the stream
- Command set — enumerate, configure, start/stop, list segments, fetch range, lock/unlock segments, time query
- Health and telemetry — storage health and remaining ring depth, drop counts, thermal, link state
- Degraded-mode and error behaviour — what a node does when it loses the controller, loses the trigger, or fills storage
- Versioning and compatibility rules, and a way to test conformance

"Drop-in" is only real if it is testable. A prose description of an interface will not survive two teams implementing it independently on different silicon.

---

## 7. Ownership and in-house scope

This shapes the firmware program more than any technical choice.

**We own the imaging chain end to end** — sensor characterization, the sensor driver, ISP/IQ tuning, the per-unit calibration pipeline, and the trigger/metadata path. That is the hard-to-copy value and the reason the company exists. It is owned by construction, by building it in-house, not by contract enforcement.

**We own the firmware.** Vendors do not write it.

**Hardware is deliberately commodity and second-sourceable.** Board design is contracted work-for-hire with native files delivered at every milestone and the freedom to manufacture anywhere.

**Per-unit factory calibration is delegated.** It is commodity metrology, not moat. The factory calibrates autonomously with their own station software; we own the spec — EEPROM map, camera-model and coordinate conventions, chart/target spec, acceptance limits, golden samples, a reference dataset their station must reproduce within tolerance, and an offline verification tool we run on records rather than on the line. Per-unit raw captures come back to us, so if their solver drifts we can re-derive and field-update calibration without them.

---

## 8. Networking

- Ethernet is the control, sync and bulk-transfer fabric. Adding cameras is adding switch ports.
- The standard-tier node has dual RGMII/SGMII, which was intended to allow a daisy-chain or redundant topology. The lite node has a single Fast Ethernet port and therefore a star topology (a ~$1 three-port FE switch on the module would restore chaining if we wanted it).
- B3 carries a LAN9668-class managed TSN switch — a full 1588/TSN device that can typically emit a PTP-disciplined pulse on a config pin.
- 48 V PoE distributes power on the same cable.
- We want to avoid RJ45 for bulk reasons; the plan is a real timestamping PHY behind a compact connector, or SPE.

---

## 9. Platform notes

**Standard tier.** AM62A3, quad Cortex-A53, LPDDR4, Wave5 hardware H.264/H.265 encoder, TI's VPAC ISP with the VISS/DCC tuning flow. TI brings English documentation, a longevity program, and FAE support. The AR0234 is not in TI's supported-sensor list, so a V4L2 driver and an IQ tuning pass are ours to do.

**Lite tier.** RV1106, single Cortex-A7 at 1.2 GHz with 128–256 MB stacked DDR3L, 5 MP ISP, H.265 encode to 3072×1728@30, dual 2-lane MIPI CSI D-PHY combinable to one 4-lane port. Openly available SDK, which matters as an escape hatch. What we lose versus TI is headroom, not function: no room for fat pre-trigger raw buffers or heavy on-camera CV, and Rockchip is vendor-kernel-plus-community territory rather than a supported toolchain. RV1126 is the mid-step if RV1106 proves tight. Note the encoder headroom: 1920×1200 at 60 fps is ~138 Mpix/s against the RV1106's rated ~159 Mpix/s, so the lite tier meets the standard-tier frame rate with very little margin. Whether that holds in practice is worth confirming early.

**Gateway.** RV1126 was chosen over RK3568 for encoder purity: RK3568's encoder is 1080p-class (1920×1088), so the gateway's own camera would have to crop 112 lines relative to the fleet and break FOV/calibration uniformity. RV1126 passes 1920×1200 untouched and is the same rkisp/RKNN family as the nodes, so sensor driver, IQ files and firmware are shared. The cost is no USB3 and no PCIe.

**Sensor uniformity.** Whatever sensor wins, we standardize one sensor across both node variants. A mixed fleet breaks photometric uniformity — different QE curves and shading behaviour — which is poison for reconstruction consistency. Two maintained ISP tunings behind one optical calibration pipeline is acceptable; two sensors in one rig is not.

**Zephyr.** The nRF54L15 supervisor and B3 run Zephyr/NCS, and we want one workflow across the fleet.

---

## 10. What we are not doing

Recorded so you don't re-litigate them without cause, and so you know they were considered:

- **No FPGA.** Considered as a genlock and multi-CSI aggregation backbone; rejected as a deliberate direction.
- **No pure-Zephyr media path.** There is no single Zephyr-supported part that does dual MIPI capture + hardware H.265 + storage. Video runs on Linux SoCs; Zephyr does the deterministic control plane.
- **No real-time centralized writes.** That is the one requirement that would force a heavy aggregator, and we gave it up on purpose.
- **No PCIe/NVMe.** Power-hungry and built for throughput we don't need at these bitrates. It would only come back if a much higher-bitrate tier ever appeared.
- **No entity-listed silicon.** HiSilicon was proposed by a vendor and rejected — we intend to sell globally.
- **No full-quality live streaming over WiFi.** Proxy substream only.

---

## 11. Decision log — what is load-bearing

| Decision | Why | How firm |
|---|---|---|
| Distributed per-tile storage | The only thing that makes flexible N work with a light controller | **Firm** — the architecture collapses without it |
| Hardware trigger per frame, sensors in slave mode | Removes accumulated drift; sync becomes a wiring problem, not a software one | **Firm** |
| Trigger counter bound into stream metadata | Without it, one dropped frame silently misaligns a camera forever | **Firm** |
| Controller never in the video path | Keeps the control plane on a small MCU and bounds its load | **Firm** |
| eMMC | Managed NAND, no connector, wear levelling handled | **Firm** |
| H.265 | AV1 has no low-power hardware encoders yet | Firm now, revisit later |
| Segment rotation for the ring | Flash-friendly, indexable, cooperates with the FTL | Strong preference |
| Two tiers behind one interface | Lets us offer a price ladder without forking the fleet software | Strong preference — the *mechanism* is open |
| gPTP between packs, edge fanout within | Best sync per unit of effort at both scales | Strong preference |
| Supervisor MCU on lite only | TI has native 1588; Rockchip does not | **Open** — see §12 |
| AM62A3 as the standard node | Chosen before the J722S EVM was on the bench | **Open** — see §12 |

---

## 12. Open questions — decide these yourself, tell us what you assumed

Nobody is going to answer these for you during the trial. Where they affect your work, pick a position, write it down, and move.

1. **What silicon the gateway and the nodes actually run.** The EVM is a J722S/AM67A — a larger sibling of the AM62A3, same Wave5 encoder and the same TI V4L2/GStreamer stack, so work done on it transfers, but with four CSI-2 connectors across two CSI2RX instances, PCIe and USB3 on top. That makes it a reasonable B2 candidate and an oversized B1. Two things follow that we have not settled: whether the gateway should be a materially bigger part than the nodes or the same part with more populated, and — because four CSI-2 inputs sit on one SoC — whether a production node is one SoC per camera at all, or one SoC ingesting several. A multi-imager node changes the tile model, the sync topology and the cost curve together.
2. **Should the supervisor MCU be back-ported onto the standard tier too?** Our current position is no — TI can do it natively, and an MCU costs BOM and a second codebase. The argument for yes is that both tiers would then present a literally identical control-plane implementation rather than two implementations of one spec. We have not decided. This is a good thing for you to have an opinion about.
3. **What is the actual sync budget in microseconds?** We have said "milliseconds is sufficient, tighter is better" and never pinned it. The number determines how much of the timing design is necessary versus nice.
4. **Ring depth and per-camera bitrate targets** for the first product. §2 gives the envelope, not a commitment.
5. **The orchestration link on the lite tier** — RS-485 on the existing cable, or nRF-to-nRF 2.4 GHz time transfer with scheduled triggering. The second is more interesting and leaves the cable spec untouched; it is also unproven for us.
6. **Update, rollback and secure identity** are named but not designed.
7. **Which sensor wins the A/B**, and therefore which V4L2 driver and IQ tuning effort we own first.

---

## 13. Glossary

- **FSIN** — frame sync input; the trigger pin that starts a sensor exposure in slave mode
- **Tile / node** — one camera's worth of {sensor, SoC, encode, storage}
- **Pack** — a co-located group of ~10 tiles sharing one electrically fanned-out trigger
- **Supervisor** — the small MCU in front of a lite node
- **Orchestrator** — the controller that commands the fleet
- **Ring / segment** — the loop recording buffer and its rotating fixed-size files
- **ICD** — the interface control document defining the orchestrator ↔ node contract
