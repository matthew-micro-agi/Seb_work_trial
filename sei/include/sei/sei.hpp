// The camsync SEI and the Annex-B helpers around it: build the SEI NAL, insert it into an access unit,
// split a byte stream into access units, parse the record back. Byte-identical to the Python twin
// sei/python/sei.py; both are tested on sei/tests/vectors.txt.
//
// Record (payload after the 16-byte UUID): "CSYN", version 1, then big-endian
// epoch:u16, k:u32, frame_ts_ns:u64, sensor_count:u16, flags:u16.
//
// Two codecs, one record. The recording is H.265; the proxy substream of DESIGN_RING_CONTROL.md §8.3a is
// H.264, because Foxglove decodes through WebCodecs. Only the NAL syntax around the payload differs, so
// every function takes a Codec and defaults to H265 — the recording path reads exactly as it did.
#pragma once
#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>
#include "kmatch/types.hpp"

namespace sei {

enum class Codec : uint8_t { H265 = 0, H264 = 1 };

struct Record {
  uint16_t epoch = 0; uint32_t k = 0; uint64_t frame_ts_ns = 0; uint16_t sensor_count = 0; uint16_t flags = 0;
};
static_assert(kmatch::FAULT_K_LOCAL <= 0x8000u, "every kmatch fault bit must fit the 16-bit SEI flags field");

// One NAL unit inside a buffer, start code excluded.
struct Nal { size_t offset; size_t size; };
// One access unit: the byte range [begin, end) of the buffer and its NALs (buffer offsets).
struct Au { size_t begin; size_t end; std::vector<Nal> nals; };

// H.265: a 2-byte header, type in bits 1..6 of the first byte, VCL < 32, IDR 19/20, prefix SEI 39.
// H.264: a 1-byte header, type in the low 5 bits, VCL 1..5, IDR 5, SEI 6.
inline uint8_t nal_type(const uint8_t* nal, Codec c = Codec::H265) {
  return c == Codec::H264 ? uint8_t(nal[0] & 0x1f) : uint8_t((nal[0] >> 1) & 0x3f);
}
inline bool is_vcl(const uint8_t* nal, Codec c = Codec::H265) {
  uint8_t t = nal_type(nal, c);
  return c == Codec::H264 ? (t >= 1 && t <= 5) : t < 32;
}
inline bool is_idr(const uint8_t* nal, Codec c = Codec::H265) {
  uint8_t t = nal_type(nal, c);
  return c == Codec::H264 ? t == 5 : (t == 19 || t == 20);
}
// The parameter set that must open a stream: VPS (H.265) or SPS (H.264).
inline bool is_first_param_set(const uint8_t* nal, Codec c = Codec::H265) { return nal_type(nal, c) == (c == Codec::H264 ? 7 : 32); }

// Annex-B NAL unit (4-byte start code included) carrying the record.
std::vector<uint8_t> build_nal(const Record& r, Codec c = Codec::H265);
// The record, if `nal` (start code excluded) is our SEI.
std::optional<Record> parse_nal(const uint8_t* nal, size_t len, Codec c = Codec::H265);
// Insert the SEI into an Annex-B access unit before its first VCL NAL (after VPS/SPS/PPS/AUD). The AU's
// own bytes are kept as they are (the Python twin re-serializes with 4-byte start codes instead).
std::vector<uint8_t> insert(const uint8_t* au, size_t len, const std::vector<uint8_t>& sei_nal, Codec c = Codec::H265);

// Split a complete Annex-B buffer into access units. A new AU starts at a first-slice VCL NAL, or at a
// VPS/SPS/PPS/AUD/prefix-SEI once the current AU holds a VCL NAL (the rule of the Python twin). The last
// AU runs to the end of the buffer whether or not it is complete.
std::vector<Au> split(const uint8_t* data, size_t len, Codec c = Codec::H265);
// End of the last AU that is followed by another one, i.e. is provably complete; 0 if there is none.
size_t last_complete_au_end(const uint8_t* data, size_t len, Codec c = Codec::H265);

// What a segment writer or indexer asks about one AU.
struct AuInfo { bool idr = false; bool has_vps = false; std::optional<Record> record; };
AuInfo inspect(const uint8_t* data, const Au& au, Codec c = Codec::H265);

}  // namespace sei
