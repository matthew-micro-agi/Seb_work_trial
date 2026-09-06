# Next steps

Summary of the unfinished implementation and validation, in priority order:

- **First, prove frame identity and expose recording loss.** Verify each frame's trigger counter (K) using LEDs and an independent electrical witness; strengthen verification, measure timing, tune buffering, persist all losses, and ensure every segment decodes independently.
- **Make timing, retention and recovery reliable.** Define clock/restart behavior, improve trigger control and timestamps, validate startup/shutdown, guarantee acknowledged event coverage, preserve indexes across reboots, and test power cuts and offload.
- **Resolve platform decisions.** Validate sensor/ISP quality and metadata preservation, finalize board/IMU interfaces, measure compute/storage/power needs, and establish reproducible builds and secure updates.
- **Build automated validation and handover evidence.** Convert requirements into tests, scale hardware testing from two to roughly ten cameras, run long-duration and fault tests, agree acceptance limits and owners, and document setup from a blank board.

The central priority is demonstrating correct frame identity and making every recording loss visible before expanding platform scope.

