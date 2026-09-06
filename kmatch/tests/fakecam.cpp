#include "fakecam.hpp"

#include <algorithm>
#include <cstdint>
#include <sstream>

namespace fakecam {

std::map<int64_t, Fault> parse_faults(const std::string& spec) {
  static const std::map<std::string, Kind> kinds = {
      {"skip", Kind::Skip}, {"drop", Kind::Drop}, {"dup", Kind::Dup},
      {"late", Kind::Late}, {"restart", Kind::Restart}, {"lostcap", Kind::LostCap}};
  std::map<int64_t, Fault> out;
  std::stringstream ss(spec);
  std::string item;
  while (std::getline(ss, item, ',')) {
    if (item.empty()) continue;
    auto at = item.find('@');
    auto colon = item.find(':', at);
    std::string kind = item.substr(0, at);
    int64_t e = std::stoll(item.substr(at + 1, colon == std::string::npos ? std::string::npos : colon - at - 1));
    double arg = colon == std::string::npos ? 0 : std::stod(item.substr(colon + 1));
    out[e] = Fault{kinds.at(kind), arg};
  }
  return out;
}

std::vector<Ev> generate(const Config& cfg) {
  auto faults = parse_faults(cfg.faults);
  std::vector<Ev> ev;
  kmatch::ns_t t = cfg.t0;
  uint32_t seq = cfg.seq_start, fs = cfg.seq_start;
  int64_t skip_until = -1;
  kmatch::ns_t last_delivered = INT64_MIN;
  for (int64_t e = 0; e < cfg.edges; ++e, t += cfg.period) {
    Fault f = faults.count(e) ? faults[e] : Fault{};
    if (f.kind == Kind::Restart) t += static_cast<kmatch::ns_t>((f.arg > 0 ? f.arg : 1000) * 1e6);
    if (f.kind != Kind::LostCap) ev.push_back({Ev::Edge, t, e});
    if (f.kind == Kind::Skip) skip_until = e + std::max<int64_t>(1, static_cast<int64_t>(f.arg)) - 1;
    if (e <= skip_until) continue;      // no exposure
    ++fs;                                // the receiver sees a frame start
    if (f.kind == Kind::Drop) continue;  // but the buffer never reaches us
    kmatch::ns_t tf = t + cfg.latency;
    if (f.kind == Kind::Late) tf += static_cast<kmatch::ns_t>(f.arg * 1e6);
    for (int copy = 0; copy < (f.kind == Kind::Dup ? 2 : 1); ++copy) {
      kmatch::FrameIn fr;
      fr.t_frame = std::max(tf, last_delivered + 500'000);  // delivered in order
      last_delivered = fr.t_frame;
      fr.seq = seq++;
      fr.frame_start = fs - 1;
      fr.has_frame_start = cfg.frame_start;
      ev.push_back({Ev::Frame, fr.t_frame, e, fr});
    }
  }
  for (kmatch::ns_t tk = cfg.t0 + cfg.period / 2; tk < t + cfg.period; tk += cfg.period)
    ev.push_back({Ev::Tick, tk, -1});
  std::stable_sort(ev.begin(), ev.end(), [](const Ev& a, const Ev& b) { return a.t < b.t; });
  return ev;
}

}  // namespace fakecam
