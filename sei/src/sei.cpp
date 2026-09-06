#include "sei/sei.hpp"
#include <cstring>

namespace sei {
namespace {
const uint8_t UUID[16] = {0x6a,0x8f,0x2b,0x4e,0x5d,0x3c,0x4a,0x1b,0x9e,0x7f,0x0c,0x2d,0x4b,0x6a,0x8e,0x10};
constexpr size_t RECORD_SIZE = 4 + 1 + 2 + 4 + 8 + 2 + 2;

void put_be(std::vector<uint8_t>& v, uint64_t x, int bytes) { for (int i = bytes - 1; i >= 0; i--) v.push_back(uint8_t(x >> (8 * i))); }
uint64_t get_be(const uint8_t* p, int bytes) { uint64_t x = 0; for (int i = 0; i < bytes; i++) x = (x << 8) | p[i]; return x; }

// Emulation prevention: after two zero bytes, a byte <= 3 gets a 0x03 in front. The zero run restarts
// after the inserted byte, as in the spec (and as the Python regex behaves).
std::vector<uint8_t> add_epb(const std::vector<uint8_t>& in) {
  std::vector<uint8_t> out; out.reserve(in.size() + 8); int zeros = 0;
  for (uint8_t b : in) {
    if (zeros >= 2 && b <= 3) { out.push_back(3); zeros = 0; }
    out.push_back(b); zeros = (b == 0) ? zeros + 1 : 0;
  }
  return out;
}
std::vector<uint8_t> strip_epb(const uint8_t* p, size_t n) {
  std::vector<uint8_t> out; out.reserve(n);
  for (size_t i = 0; i < n; i++) {
    if (i + 2 < n && p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 3) { out.push_back(0); out.push_back(0); i += 2; continue; }
    out.push_back(p[i]);
  }
  return out;
}
size_t find_sc3(const uint8_t* d, size_t len, size_t from) {
  for (size_t i = from; i + 3 <= len; i++) if (d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 1) return i;
  return len;
}
// first_mb_in_slice / first_slice_segment_in_pic_flag is the first bit after the header, so the byte
// after the NAL header has its top bit set exactly on the picture's first slice.
bool first_slice(const uint8_t* nal, size_t n, Codec c) {
  const size_t hdr = c == Codec::H264 ? 1u : 2u;
  return is_vcl(nal, c) && n > hdr && (nal[hdr] & 0x80);
}
// A NAL that opens a new access unit once the current one already holds a slice.
bool au_starter(uint8_t t, Codec c) {
  return c == Codec::H264 ? (t == 6 || t == 7 || t == 8 || t == 9)          // SEI, SPS, PPS, AUD
                          : (t == 32 || t == 33 || t == 34 || t == 35 || t == 39);   // VPS, SPS, PPS, AUD, prefix SEI
}
uint8_t sei_nut(Codec c) { return c == Codec::H264 ? 6 : 39; }
size_t hdr_bytes(Codec c) { return c == Codec::H264 ? 1u : 2u; }
}  // namespace

std::vector<uint8_t> build_nal(const Record& r, Codec c) {
  std::vector<uint8_t> payload(UUID, UUID + 16);
  payload.insert(payload.end(), {'C','S','Y','N'}); payload.push_back(1);              // magic, version
  put_be(payload, r.epoch, 2); put_be(payload, r.k, 4); put_be(payload, r.frame_ts_ns, 8);
  put_be(payload, r.sensor_count, 2); put_be(payload, r.flags, 2);
  std::vector<uint8_t> body{5, uint8_t(payload.size())};
  body.insert(body.end(), payload.begin(), payload.end()); body.push_back(0x80);        // rbsp trailing bits
  std::vector<uint8_t> nal{0, 0, 0, 1};
  if (c == Codec::H264) nal.push_back(6);                                               // nal_ref_idc 0, SEI
  else { nal.push_back(uint8_t(39 << 1)); nal.push_back(1); }                           // PREFIX_SEI_NUT, layer 0, tid 1
  auto esc = add_epb(body); nal.insert(nal.end(), esc.begin(), esc.end());
  return nal;
}

std::optional<Record> parse_nal(const uint8_t* nal, size_t len, Codec c) {
  const size_t hdr = hdr_bytes(c);
  if (len < hdr + 1 || nal_type(nal, c) != sei_nut(c)) return std::nullopt;
  auto body = strip_epb(nal + hdr, len - hdr);
  if (body.size() < 2 || body[0] != 5) return std::nullopt;
  size_t plen = body[1];
  if (body.size() < 2 + plen || plen < 16 + RECORD_SIZE) return std::nullopt;
  const uint8_t* p = body.data() + 2;
  if (memcmp(p, UUID, 16) != 0 || memcmp(p + 16, "CSYN", 4) != 0 || p[20] != 1) return std::nullopt;
  Record r; p += 21;
  r.epoch = uint16_t(get_be(p, 2)); r.k = uint32_t(get_be(p + 2, 4)); r.frame_ts_ns = get_be(p + 6, 8);
  r.sensor_count = uint16_t(get_be(p + 14, 2)); r.flags = uint16_t(get_be(p + 16, 2));
  return r;
}

std::vector<uint8_t> insert(const uint8_t* au, size_t len, const std::vector<uint8_t>& sei_nal, Codec c) {
  // the first VCL NAL; the SEI goes right before its start code
  size_t i = 0, insert_at = len;
  while (i + 3 <= len) {
    if (au[i] == 0 && au[i + 1] == 0 && (au[i + 2] == 1 || (i + 3 < len && au[i + 2] == 0 && au[i + 3] == 1))) {
      size_t sc = (au[i + 2] == 1) ? 3 : 4; size_t hdr = i + sc;
      size_t sc_start = (sc == 3 && i > 0 && au[i - 1] == 0) ? i - 1 : i;   // zero_byte belongs to the start code
      if (hdr < len && is_vcl(au + hdr, c)) { insert_at = sc_start; break; }
      i = hdr;
    } else i++;
  }
  std::vector<uint8_t> out; out.reserve(len + sei_nal.size());
  out.insert(out.end(), au, au + insert_at); out.insert(out.end(), sei_nal.begin(), sei_nal.end()); out.insert(out.end(), au + insert_at, au + len);
  return out;
}

std::vector<Au> split(const uint8_t* d, size_t len, Codec c) {
  std::vector<Au> aus; Au cur{0, 0, {}}; bool has_vcl = false; size_t prev_end = 0;
  size_t i = find_sc3(d, len, 0);
  while (i < len) {
    size_t j = find_sc3(d, len, i + 3);
    size_t nb = i + 3, ne = j;
    while (ne > nb && d[ne - 1] == 0) ne--;                          // zero_byte of a following 4-byte start code
    size_t sc_start = (i > prev_end && d[i - 1] == 0) ? i - 1 : i;   // where this NAL's start code begins
    if (ne > nb) {
      const uint8_t* nal = d + nb; size_t n = ne - nb;
      if (has_vcl && (first_slice(nal, n, c) || au_starter(nal_type(nal, c), c))) {
        cur.end = sc_start; aus.push_back(cur); cur = Au{sc_start, 0, {}}; has_vcl = false;
      }
      cur.nals.push_back(Nal{nb, n});
      has_vcl = has_vcl || is_vcl(nal, c);
      prev_end = ne;
    }
    i = j;
  }
  if (!cur.nals.empty()) { cur.end = len; aus.push_back(cur); }
  return aus;
}

size_t last_complete_au_end(const uint8_t* d, size_t len, Codec c) {
  auto aus = split(d, len, c);
  return aus.size() >= 2 ? aus[aus.size() - 2].end : 0;
}

AuInfo inspect(const uint8_t* d, const Au& au, Codec c) {
  AuInfo info;
  for (const Nal& n : au.nals) {
    const uint8_t* p = d + n.offset;
    if (is_first_param_set(p, c)) info.has_vps = true;
    if (is_idr(p, c)) info.idr = true;
    if (!info.record && nal_type(p, c) == sei_nut(c)) info.record = parse_nal(p, n.size, c);
  }
  return info;
}

}  // namespace sei
