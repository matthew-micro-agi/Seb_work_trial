#include "kmatch/types.hpp"

namespace kmatch {

std::string fault_names(uint32_t faults) {
  static const struct { uint32_t bit; const char* name; } table[] = {
      {FAULT_DUPLICATE, "DUPLICATE"},         {FAULT_DELIVERY_LOST, "DELIVERY_LOST"},
      {FAULT_SENSOR_MISSED, "SENSOR_MISSED"}, {FAULT_LATE, "LATE"},
      {FAULT_UNMATCHED, "UNMATCHED"},         {FAULT_EPOCH_CHANGE, "EPOCH_CHANGE"},
      {FAULT_SEQ_REGRESSION, "SEQ_REGRESSION"}, {FAULT_EDGE_PENDING, "EDGE_PENDING"},
      {FAULT_K_CORRECTED, "K_CORRECTED"}, {FAULT_K_LOCAL, "K_LOCAL"},
  };
  std::string out;
  for (const auto& e : table) {
    if (faults & e.bit) {
      if (!out.empty()) out += '|';
      out += e.name;
    }
  }
  return out.empty() ? "OK" : out;
}

}  // namespace kmatch
