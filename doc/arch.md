# Architecture

N cameras that expose on the same instant, each recording to its own storage, gathered afterwards.
Running today on one TI J722S board with a Sony IMX296: hardware-triggered capture, H.265 to a rotating
ring of files on flash, a live view, and a network interface a laptop drives. Greyscale, one tile.

## Terms

```
                    ┌──────────────┐
                    │ orchestrator │  start, stop, fetch, monitor.
                    │ runs `orch`  │  Video only when it asks for it.
                    └──────┬───────┘
                           │ Ethernet
  ┌────────────────────────┼───────────────────────────────── pack ─────┐
  │                        │                                            │
  │   ┌───────────────┐  ┌─┴─────────────┐      ┌───────────────┐       │
  │   │ tile          │  │ tile          │      │ tile          │       │
  │   │ sensor        │  │ sensor        │ ...  │ sensor        │       │
  │   │ SoC + encoder │  │ SoC + encoder │      │ SoC + encoder │       │
  │   │ flash: ring   │  │ flash: ring   │      │ flash: ring   │       │
  │   │ agent         │  │ agent         │      │ agent         │       │
  │   └───────┬───────┘  └───────┬───────┘      └───────┬───────┘       │
  │           └──────────────────┴──────────────────────┘               │
  │                       one trigger wire                              │
  └─────────────────────────────────────────────────────────────────────┘
```

| | |
|---|---|
| **tile** | one camera: sensor, SoC, encoder, its own flash, an agent. The unit that scales |
| **pack** | tiles sharing one trigger wire and one network. About ten |
| **orchestrator** | whatever drives the pack, running `orch`. A laptop on the bench, a gateway tile in the product |
| **agent** | on each tile: serves the interface, supervises the recorder |
| **recorder** | on each tile: capture, match, encode, write |
| **ring** | the rotating segment files on a tile's flash |
| **K** | the trigger index: the number of the pulse that exposed a frame |
| **epoch** | which run of the counter a K belongs to. K restarts when the pulse source does, so `(epoch, K)` is the key that is comparable across tiles |

**The boundary moves in the product.** On the bench the orchestrator is a laptop. In the product it is the
gateway tile: the same `orch` code, running on one of the tiles, which drives the pack over the internal
network and serves the outside in turn, so an external client asks the gateway for footage, health or a
live stream and the gateway asks the tiles.

## 1. The problem

Pictures from several tiles are only useful together if you know which share a moment.

**Tiles drift.** Each sensor has its own oscillator. Left free-running at 30 fps they slide apart.

**A dropped frame is silent.** Number frames as they arrive, lose one, and everything after is off by one
forever. Nothing downstream can tell. Worse than drift, because it looks like success.

## 2. The one idea

**One wire carries a pulse to every tile. Every picture is stamped with the number of the pulse that took
it.** That number is K. Not a timestamp, not a frame count: it names a physical event, and every working
tile saw the same one.

Drift cannot accumulate, because each exposure is started by the pulse rather than an internal clock. A
lost frame cannot hide, because K jumps from 41 200 to 41 202 and the gap is in the recording.

**How a picture is matched to a pulse.** The tile keeps two lists in one clock: pulses with their indices,
and frames with their arrival times. For a new frame it subtracts a fixed delay from the arrival time and
takes the pulse nearest the result. That index becomes K; the miss distance is kept as a residual. Beyond
a quarter period the frame is marked late, and with no candidate it is marked unmatched rather than
guessed at.

**A frame says what went wrong with it.** Every frame carries fault bits into the stream: `DUPLICATE`,
`DELIVERY_LOST`, `SENSOR_MISSED`, `LATE`, `UNMATCHED`, `EPOCH_CHANGE`, `SEQ_REGRESSION`, `EDGE_PENDING`,
`K_CORRECTED`, and the standing `K_LOCAL` for footage recorded before the agent anchored the count. The
tile-level equivalent is `TRIGGER_STALLED`, raised after two missing periods: the tile stays up, closes
its segment and says so, rather than free-running at approximately the right rate.

**Good to know: the delay is one whole trigger period**, 33.36 ms at 30 fps rather than the 17 ms of
exposure and readout, because this sensor closes its frame on the wire at the next trigger instead of
after the last pixel, and the receiver stamps the buffer when the transfer ends. It is a bench problem
and a constant one, holding to within 40 µs across 200 frames, so the matcher subtracts it and K is
unaffected; a sensor that closes its frame after the last line would not have it, which is now a question
worth asking of any candidate.

## 3. One tile, in detail

```
 ┌ HARDWARE ─────────────────────────────────────────────────────────────────┐
 │                                                                           │
 │   eHRPWM0 ──── pulse ───┬──────────────────────────────► CPTS             │
 │   hardware PWM          │                                 │ latches the   │
 │                         ▼                                 │ edge, 2 ns    │
 │                  IMX296, slave mode                       │               │
 │                  one exposure per pulse                   │               │
 │                         │ CSI-2, one lane                 │               │
 │                         ▼                                 │               │
 │                  CSI2RX  (Cadence bridge + TI shim)       │               │
 │                         ▼  DMA into DDR                   │               │
 │                    ① timestamp: ktime_get_ns() at         │               │
 │                      DMA completion, CLOCK_MONOTONIC      │               │
 └─────────────────────────┼─────────────────────────────────┼───────────────┘
                           │                                 │
                  frame: bytes, t_arrival        edges: t_edge, K, converted
                                                 from CPTS to CLOCK_MONOTONIC
 ┌ RECORDER, capture thread┼─────────────────────────────────┼───────────────┐
 │                         └──────────────┬──────────────────┘               │
 │                                        ▼                                  │
 │                         ② MATCH (§2)  → K, epoch, fault bits              │
 │                                        ▼                                  │
 │                     ┌──────────────────────────────────────┐              │
 │                     │  Bayer → NV12, on one CPU core       │  ← the VPAC  │
 │                     │  10-bit >>2 into luma, chroma = 128  │     ISP goes │
 │                     │  no demosaic: hence the mosaic       │     here     │
 │                     └──────────────────────────────────────┘              │
 │                                        ▼                                  │
 │                         ③ attach K to the buffer as metadata              │
 └────────────────────────────────────────┼──────────────────────────────────┘
                                          ▼
 ┌ GSTREAMER ─────────────────────── frame source ───────────────────────────┐
 │                                       tee                                 │
 │                  ┌─────────────────────┴─────────────────────┐            │
 │             RECORDING                                     PROXY           │
 │                  ▼                                          ▼             │
 │                queue                            valve  (shut unless       │
 │                  │                                ▼      anyone watches)  │
 │                  │                           drop 1 in 2                  │
 │                  │                                ▼                       │
 │                  │                           videorate  15 fps            │
 │                  │                                ▼                       │
 │                  │                           videoscale 728×544           │
 │                  │                                │  ← the 2:1 average    │
 │                  │                                │    cancels the mosaic │
 │                  ▼                                ▼                       │
 │         ④ probe: metadata → this branch's table, keyed by frame time      │
 │                  ▼                                ▼                       │
 │            v4l2h265enc                      v4l2h264enc     both Wave5,   │
 │              6 Mbps                           1 Mbps        two instances │
 │                  ▼                                ▼                       │
 │             h265parse                        h264parse                    │
 │                  ▼                                ▼                       │
 │         ⑤ probe: table → K written into the frame as an SEI record        │
 │                  ▼                                ▼                       │
 │           segment writer                    unix socket                   │
 └───────────────────────────────────────────────────────────────────────────┘
```

**eHRPWM0** is a hardware PWM: configured once, then running on its own, so the pulses owe nothing to CPU
load. Measured interval 33 333 760 ns, no variation the hardware can detect. **CPTS** is the 1588
timestamping block in the Ethernet switch; one of its capture inputs takes the same pulse over a jumper,
so the tile counts pulses it *receives*, not ones it believes it sent. That matters when the pulse comes
from another board and this tile is only listening.

**① is where the one-period delay enters**, and it is the only timestamp the driver offers.

**③ to ⑤ is how the picture and its index stay together.** The metadata rides the raw frame and survives
every step except an encoder, which discards it. Each branch therefore bridges its own encoder: one probe
puts the index into a small table on the way in, another takes it out and writes it into the compressed
frame. Two branches carry the same K while sharing nothing.

**The boxed conversion is the one CPU step in the picture path**, and what the ISP would replace.

### After the pipeline

```
        segment writer                            unix socket
              │  4 s per file                          │  one message per frame
              ▼                                        ▼
   ring: /ring/seg/000000000123.h265        ┌────────────────────────┐
   ext4 on the eMMC                         │ agent fan-out          │
              │  "segment closed"           │ one reader, N viewers  │
              ▼                             └───────────┬────────────┘
   ┌──────────────────────────┐                         │
   │ agent indexer            │                         │
   │ first and last record    │                         │
   │ → one row in the index   │                         │
   │ (a cache, rebuildable    │                         │
   │  from the files alone)   │                         │
   └────────────┬─────────────┘                         │
                ▼                                       ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ agent, HTTP on port 8080                                    │
   │   ListSegments   what is stored     live      proxy stream  │
   │   .../data       bytes, resumable   PutLock   keep a range  │
   └────────────────────────────┬────────────────────────────────┘
                                │ Ethernet
                                ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ orch, on the orchestrator                                   │
   │   fetch  → files, verified      bridge → Foxglove           │
   │   mcap   → one file offline                                 │
   └─────────────────────────────────────────────────────────────┘
```

The two paths never meet again on the tile. Recorded video goes to flash and is fetched later; live video
never touches flash. Both carry K in the same format, so one tool reads either.

