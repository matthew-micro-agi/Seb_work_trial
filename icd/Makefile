# icd — tile.proto and what is generated from it.
#
#   make            regenerate py/tile_pb2.py (protoc on the PC; the result is committed, the board has no protoc)
#   make check      fail if the committed py/tile_pb2.py is stale
#   make test       run the conformance suite against HOST (default 192.168.168.2:8080)
#
# vendor/ is the pure-Python protobuf runtime (protobuf 6.33.6, py3-none-any wheel unpacked, testdata and
# pyext removed; LICENSE-protobuf). py/tilev1.py falls back to it when the system has no google.protobuf.
# To bump: pip download protobuf==X --no-deps --only-binary=:all: --platform any -d /tmp/w &&
# unzip the wheel's google/ over vendor/google.
PROTOC ?= protoc
HOST ?= 192.168.168.2:8080

py/tile_pb2.py: tile.proto
	$(PROTOC) -I . --python_out=py tile.proto

.PHONY: check test
check:
	@$(PROTOC) -I . --python_out=/tmp tile.proto && diff -q /tmp/tile_pb2.py py/tile_pb2.py && echo "py/tile_pb2.py is current"

test:
	python3 conformance.py $(HOST)
