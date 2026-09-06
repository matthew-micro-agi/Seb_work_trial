#include "livesock.hpp"
#include <cerrno>
#include <cstring>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

bool LiveSock::open(const std::string& path, std::string& err) {
  std::lock_guard<std::mutex> g(lock_);
  if (path.size() >= sizeof(sockaddr_un::sun_path)) { err = "socket path too long"; return false; }
  ::unlink(path.c_str());
  int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
  if (fd < 0) { err = std::string("socket: ") + strerror(errno); return false; }
  sockaddr_un a{}; a.sun_family = AF_UNIX; memcpy(a.sun_path, path.c_str(), path.size());
  if (::bind(fd, reinterpret_cast<sockaddr*>(&a), sizeof a) < 0 || ::listen(fd, 8) < 0) {
    err = std::string("bind ") + path + ": " + strerror(errno); ::close(fd); return false;
  }
  ::chmod(path.c_str(), 0660);
  fd_ = fd; path_ = path;
  return true;
}

void LiveSock::close() {
  std::lock_guard<std::mutex> g(lock_);
  for (Reader& r : readers_) { ::shutdown(r.fd, SHUT_WR); ::close(r.fd); }   // an orderly EOF, not a reset
  readers_.clear();
  if (fd_ >= 0) { ::close(fd_); ::unlink(path_.c_str()); fd_ = -1; }
}

void LiveSock::flush(Reader& r) {
  while (!r.pending.empty()) {
    ssize_t n = ::send(r.fd, r.pending.data(), r.pending.size(), MSG_NOSIGNAL);
    if (n > 0) { r.pending.erase(0, size_t(n)); continue; }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;   // reader is busy; try again next AU
    r.pending.assign(kBacklogCap + 1, '\0');                          // gone: mark for the drop in write()
    return;
  }
}

void LiveSock::write(const uint8_t* data, size_t len, bool keyframe) {
  char hdr[kHeader] = {'K', 'A', 'U', '1'};
  for (int i = 0; i < 4; i++) hdr[4 + i] = char((len >> (8 * (3 - i))) & 0xff);
  hdr[11] = keyframe ? 1 : 0;
  std::lock_guard<std::mutex> g(lock_);
  if (fd_ < 0) return;
  for (;;) {
    int c = ::accept4(fd_, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (c < 0) break;
    readers_.push_back(Reader{c, {}});
  }
  for (size_t i = 0; i < readers_.size();) {
    Reader& r = readers_[i];
    if (r.pending.size() <= kBacklogCap) {
      r.pending.append(hdr, kHeader);
      r.pending.append(reinterpret_cast<const char*>(data), len);
    }
    flush(r);
    if (r.pending.size() > kBacklogCap) { ::close(r.fd); readers_.erase(readers_.begin() + long(i)); dropped_++; continue; }
    i++;
  }
}

int LiveSock::clients() {
  std::lock_guard<std::mutex> g(lock_);
  return int(readers_.size());
}

uint64_t LiveSock::dropped() {
  std::lock_guard<std::mutex> g(lock_);
  return dropped_;
}
