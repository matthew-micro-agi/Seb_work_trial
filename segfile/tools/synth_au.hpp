// A synthetic H.265 access unit: VPS/SPS/PPS on IDRs, the camsync SEI, one slice NAL whose payload holds
// no 00 00 0x sequence, so the AU splitter sees exactly these NALs. Shared by kpipe-synth and the tests.
#pragma once
#include <cstdint>
#include <initializer_list>
#include <vector>
#include "sei/sei.hpp"

inline std::vector<uint8_t> synth_au(bool idr, size_t payload, const sei::Record& r, uint32_t seed) {
  static const uint8_t SC[4] = {0, 0, 0, 1};
  std::vector<uint8_t> au;
  auto nal = [&](std::initializer_list<uint8_t> hdr) { au.insert(au.end(), SC, SC + 4); au.insert(au.end(), hdr.begin(), hdr.end()); };
  if (idr) { nal({32 << 1, 1, 0x0c, 0x01, 0xff, 0xff}); nal({33 << 1, 1, 0x01, 0x01, 0x60}); nal({34 << 1, 1, 0xc0, 0xf2}); }
  auto s = sei::build_nal(r); au.insert(au.end(), s.begin(), s.end());
  nal({uint8_t((idr ? 19 : 1) << 1), 1, 0xaf});           // first_slice_segment_in_pic_flag set
  for (size_t i = 0; i < payload; i++) { seed = seed * 1103515245u + 12345u; au.push_back(uint8_t(0x10 + (seed >> 24) % 0xf0)); }
  au.push_back(0x80);
  return au;
}
