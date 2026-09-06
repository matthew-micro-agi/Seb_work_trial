#include "v4l2cap.hpp"
#include <cerrno>
#include <cstring>
#include <stdexcept>
#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <linux/videodev2.h>

static void xioctl(int fd, unsigned long req, void* arg, const char* what) {
  if (ioctl(fd, req, arg) < 0) throw std::runtime_error(std::string(what) + ": " + strerror(errno));
}

V4l2Capture::V4l2Capture(const std::string& dev, unsigned width, unsigned height, uint32_t fourcc, unsigned nbuf)
    : w_(width), h_(height) {
  fd_ = open(dev.c_str(), O_RDWR | O_NONBLOCK);
  if (fd_ < 0) throw std::runtime_error(dev + ": " + strerror(errno));
  v4l2_format fmt{}; fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  fmt.fmt.pix.width = width; fmt.fmt.pix.height = height; fmt.fmt.pix.pixelformat = fourcc; fmt.fmt.pix.field = V4L2_FIELD_NONE;
  xioctl(fd_, VIDIOC_S_FMT, &fmt, "VIDIOC_S_FMT");
  if (fmt.fmt.pix.width != width || fmt.fmt.pix.height != height || fmt.fmt.pix.pixelformat != fourcc)
    throw std::runtime_error("driver changed the format");
  stride_ = fmt.fmt.pix.bytesperline;
  v4l2_requestbuffers req{}; req.count = nbuf; req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE; req.memory = V4L2_MEMORY_MMAP;
  xioctl(fd_, VIDIOC_REQBUFS, &req, "VIDIOC_REQBUFS");
  for (unsigned i = 0; i < req.count; i++) {
    v4l2_buffer b{}; b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE; b.memory = V4L2_MEMORY_MMAP; b.index = i;
    xioctl(fd_, VIDIOC_QUERYBUF, &b, "VIDIOC_QUERYBUF");
    void* p = mmap(nullptr, b.length, PROT_READ, MAP_SHARED, fd_, b.m.offset);
    if (p == MAP_FAILED) throw std::runtime_error("mmap failed");
    maps_.emplace_back(static_cast<uint8_t*>(p), b.length);
    xioctl(fd_, VIDIOC_QBUF, &b, "VIDIOC_QBUF");
  }
}

V4l2Capture::~V4l2Capture() {
  for (auto& m : maps_) munmap(m.first, m.second);
  if (fd_ >= 0) close(fd_);
}

void V4l2Capture::start() {
  int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  for (int attempt = 0;; attempt++) {
    if (ioctl(fd_, VIDIOC_STREAMON, &type) == 0) return;
    if (attempt == 1) throw std::runtime_error(std::string("VIDIOC_STREAMON: ") + strerror(errno));
    usleep(300000);
  }
}

void V4l2Capture::stop() { int type = V4L2_BUF_TYPE_VIDEO_CAPTURE; ioctl(fd_, VIDIOC_STREAMOFF, &type); }

bool V4l2Capture::dequeue(Frame& f, int timeout_ms) {
  pollfd p{fd_, POLLIN, 0};
  if (poll(&p, 1, timeout_ms) <= 0) return false;
  v4l2_buffer b{}; b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE; b.memory = V4L2_MEMORY_MMAP;
  if (ioctl(fd_, VIDIOC_DQBUF, &b) < 0) { if (errno == EAGAIN) return false; throw std::runtime_error(std::string("VIDIOC_DQBUF: ") + strerror(errno)); }
  f.seq = b.sequence; f.ts_ns = int64_t(b.timestamp.tv_sec) * 1000000000LL + int64_t(b.timestamp.tv_usec) * 1000LL;
  f.data = maps_[b.index].first; f.size = b.bytesused; f.index = b.index;
  return true;
}

void V4l2Capture::release(const Frame& f) {
  v4l2_buffer b{}; b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE; b.memory = V4L2_MEMORY_MMAP; b.index = f.index;
  xioctl(fd_, VIDIOC_QBUF, &b, "VIDIOC_QBUF");
}
