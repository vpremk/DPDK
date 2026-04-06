# DPDK Simulation — macOS Build
# ================================
# Targets:
#   make sim      — run Python DPDK simulation (no sudo needed)
#   make build    — compile C pcap implementation
#   make run      — run C pcap on default interface (needs sudo)
#   make offline  — run C pcap against a .pcap file
#   make mc       — run Monte Carlo pricing engine
#   make all      — build + sim + mc
#   make clean    — remove build artifacts

CC      := clang
CFLAGS  := -O2 -Wall -Wextra -std=c11
LDFLAGS := -lpcap
TARGET  := ems/dpdk_pcap
PCAP_SDK:= $(shell xcrun --show-sdk-path)
IFLAGS  := -I$(PCAP_SDK)/usr/include

PYTHON  := .venv/bin/python3
IFACE   := en0          # change to your active interface (ifconfig to check)
PCAP_FILE :=            # set to replay a .pcap file offline

.PHONY: all sim build run offline mc send send-mktdata send-burst clean venv

all: build sim mc

## ── Python venv ───────────────────────────────────────────────────────
venv:
	@echo "→ Creating Python venv..."
	python3 -m venv .venv
	.venv/bin/pip install numpy scipy --quiet
	@echo "✓ venv ready"

## ── Python DPDK simulation (EMS) ─────────────────────────────────────
sim: venv
	@echo "\n→ Running DPDK Python simulation (EMS)..."
	$(PYTHON) ems/dpdk_sim.py

## ── Monte Carlo pricing (Pre-Trade Risk) ──────────────────────────────
mc: venv
	@echo "\n→ Running Monte Carlo pricing engine (pre_trade_risk)..."
	$(PYTHON) pre_trade_risk/montecarlo_pricing.py

## ── C build (EMS) ────────────────────────────────────────────────────
build: $(TARGET)

$(TARGET): ems/dpdk_pcap.c
	@echo "→ Compiling ems/dpdk_pcap.c..."
	$(CC) $(CFLAGS) $(IFLAGS) ems/dpdk_pcap.c $(LDFLAGS) -o $(TARGET)
	@echo "✓ Built: ./$(TARGET)"

## ── Live capture (needs sudo) ─────────────────────────────────────────
run: build
	@echo "→ Starting live capture on $(IFACE) (Ctrl+C to stop)..."
	@echo "  (requires sudo for raw packet access)"
	sudo ./$(TARGET) $(IFACE)

## ── Offline pcap replay ───────────────────────────────────────────────
offline: build
	@if [ -z "$(PCAP_FILE)" ]; then \
		echo "Usage: make offline PCAP_FILE=path/to/file.pcap"; \
		exit 1; \
	fi
	./$(TARGET) --offline $(PCAP_FILE)

## ── Send FIX orders (Client) ──────────────────────────────────────────
send:
	$(PYTHON) client/send_fix_orders.py --count 20 --rate 5 --verbose

send-burst:
	$(PYTHON) client/send_fix_orders.py --count 1000 --rate 0

## ── Send market data (Client) ─────────────────────────────────────────
send-mktdata:
	$(PYTHON) client/send_market_data.py --count 50 --rate 10 --verbose

## ── RDMA latency benchmark (EMS) ─────────────────────────────────────
rdma-bench:
	$(PYTHON) ems/rdma_transport.py --mode bench --iters 50000

## ── Cleanup ───────────────────────────────────────────────────────────
clean:
	rm -f $(TARGET)
	find . -name "*.o" -delete
	@echo "✓ Cleaned"

## ── Help ──────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  make sim                        — EMS: Python DPDK simulation"
	@echo "  make build                      — EMS: compile C pcap binary"
	@echo "  make run IFACE=en0              — EMS: live capture (sudo required)"
	@echo "  make offline PCAP_FILE=x.pcap   — EMS: replay pcap file"
	@echo "  make mc                         — Pre-Trade Risk: Monte Carlo pricing"
	@echo "  make send                       — Client: send 20 FIX orders at 5/s"
	@echo "  make send-burst                 — Client: send 1000 FIX orders at max rate"
	@echo "  make send-mktdata               — Client: send 50 market-data msgs at 10/s"
	@echo "  make rdma-bench                 — EMS: RDMA latency benchmark"
	@echo "  make all                        — build + sim + mc"
	@echo "  make clean                      — remove artifacts"
	@echo ""
