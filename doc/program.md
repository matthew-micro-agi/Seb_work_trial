# Program

## Staffing and platform

Two key hires, in priority order:

1. **Senior electrical engineer (FTE)**
2. **Mid-level firmware engineer, embedded Linux (FTE)**

Target one TI SoC across camera and gateway roles, subject to validating capture, ISP, encoding and gateway I/O requirements.

## Main risks and mitigation

| Risk | Mitigation and acceptance evidence |
|---|---|
| Supply-chain availability delays prototypes or production | Confirm supplier lead times, lifecycle status and availability for critical BOM parts before platform freeze. Secure prototype and pilot quantities early, qualify alternatives where feasible, and size buffer stock against replenishment lead times. Require a procurement plan with confirmed delivery dates and documented fallback options. |
| Silent frame misidentification or exposure misalignment | Compare an independently verified LED trigger counter with recorded `(epoch, K)`. Inject drops, delays and restarts. Require zero silent identity errors in qualification runs, explicit losses, and exposure skew within the agreed limit. |
| Production sensor and colour ISP integration | Start sensor bring-up and tuning immediately; confirm tooling and ownership. Demonstrate required resolution/frame rate, cross-camera image quality and preserved metadata. |
| Lost recordings or protected events | Define event-lock guarantees; test power cuts, full storage and concurrent retrieval/reclamation. Verify protected coverage, independent segment decoding and index recovery. |
| Pack throughput, power and thermal limits | Scale from two to roughly ten cameras early. Test recording, proxies and offload together against session, retention, offload-time and thermal targets. |
| Staffing and hardware dependencies delay delivery | Prioritize the senior electrical hire, followed by the embedded Linux firmware hire, and secure boards, sensors and test fixtures. Establish reproducible builds and automated validation from the start. |

## Milestones

Dates are targets conditional on staffing and hardware availability; completion requires the acceptance evidence.

| Target | Acceptance gate |
|---|---|
| **4 weeks** | Roughly ten-camera engineering pack on available hardware: independently verified frame identity and exposure alignment, working event retention and verified offload. Pass an 8-hour recording run plus drop/restart tests. Demonstrate the production sensor's colour ISP path; freeze platform and board interfaces and secure pilot components. |
| **3 months** | Pilot-ready pack on custom hardware with tuned production colour, validated camera/IMU timing and calibration, and recording/proxy/offload concurrency. Pass 24-hour recording and power/storage/network fault tests. Demonstrate provisioning, signed updates, rollback and factory calibration verification; deploy initial pilot units. |
| **6 months** | Release the first production batch after closing pilot findings. Demonstrate repeatable manufacturing acceptance, calibration traceability, field updates and recovery, and multi-pack synchronization against agreed limits. Complete production provisioning and handover; confirm component coverage for the next build. |

## Critical path and first decisions

Run timing/frame-identity validation and production sensor/ISP integration in parallel; both must pass before pack qualification.