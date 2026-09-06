// Minimal test harness: CHECK / CHECK_EQ, a TEST registry, main() runs them all.
#pragma once

#include <cstdio>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace check {

template <class T>
std::ostream& operator<<(std::ostream& o, const std::vector<T>& v) {
  o << "{";
  for (size_t i = 0; i < v.size(); ++i) o << (i ? "," : "") << v[i];
  return o << "}";
}

struct Case { const char* name; std::function<void()> fn; };
inline std::vector<Case>& cases() { static std::vector<Case> c; return c; }
inline int& failures() { static int f = 0; return f; }
inline const char*& current() { static const char* n = ""; return n; }

struct Reg { Reg(const char* n, std::function<void()> f) { cases().push_back({n, std::move(f)}); } };

template <class A, class B>
void eq(const A& a, const B& b, const char* ea, const char* eb, const char* file, int line) {
  if (!(a == b)) {
    std::ostringstream o;
    o << file << ":" << line << " [" << current() << "] " << ea << " == " << eb
      << "  got " << a << " vs " << b;
    std::fprintf(stderr, "FAIL %s\n", o.str().c_str());
    ++failures();
  }
}

inline int run() {
  for (auto& c : cases()) {
    current() = c.name;
    int before = failures();
    c.fn();
    std::fprintf(stderr, "%s %s\n", failures() == before ? " ok " : "FAIL", c.name);
  }
  std::fprintf(stderr, "%zu tests, %d failures\n", cases().size(), failures());
  return failures() ? 1 : 0;
}

}  // namespace check

#define TEST(name) \
  static void name(); \
  static check::Reg reg_##name(#name, name); \
  static void name()
#define CHECK_EQ(a, b) check::eq((a), (b), #a, #b, __FILE__, __LINE__)
#define CHECK(c) check::eq(bool(c), true, #c, "true", __FILE__, __LINE__)
#define CHECK_MAIN int main() { return check::run(); }
