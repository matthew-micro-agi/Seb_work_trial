
In one sentence, my standpoint: 
“Simplify the platform to one silicon and one software stack; prove frame identity and production image
  quality before expanding the hardware variants.”
  
# Main discussion points

- **Trigger MCU:** TI handles the current shared trigger. Do deployments need different exposure durations per camera with aligned exposure midpoints? If so, evaluate a central MCU with synchronized, centre-aligned PWM outputs. Set the exposure-command deadline and account for wiring, cost and firmware ownership.

- **One silicon:** Choose one TI SoC for camera and gateway roles: no Rockchip and no second TI variant. Confirm that it meets capture, ISP, encoding and gateway I/O needs; accept the unit-cost tradeoff for one BSP and software stack.

- **Off-pack data path:** Define how footage leaves the pack: Wi-Fi upload, attached mass storage, or both. Which board owns these interfaces? Does the gateway buffer footage or relay it from tiles? Set throughput/offload-time targets, concurrent recording requirements, and when verified copies allow local deletion.

- **ISP and tuning:** Deliver a usable colour pipeline for the production sensor. Who owns sensor integration, ISP bring-up, image-quality tuning and calibration: us, TI, the sensor vendor or a specialist? Agree tooling/access, captures and editable tuning deliverables, acceptance scenes, and ongoing maintenance. Coordinate per-camera exposure/gain with cross-camera colour consistency.

- **Camera and IMU calibration** Intrinsics/extrinsics, procedure for N-cam. Mounting. 

- **IMU sync** 

- **Hardware test rig:** 
For Sync:
Most critical aspect of the architecture is:
Does global exposure index K get matched to frame of that exposure?
We watermark the scene (leds displaying K in binary) and compare that to the index meta data of the frame (this is what our matching node assigns)
Follow up automation: put leds on april tag. Then simple opencv job to read out K from leds and check: 
K_led == K_frame_meta_data?
Another for in-house isp tuning?

- **Production software:** Reproducible builds, signed updates, rollback, device provisioning and recovery. Prove installation on a blank board and recovery after interrupted updates.
