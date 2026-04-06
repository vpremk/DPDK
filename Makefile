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
TARGET  := dpdk_pcap
PCAP_SDK:= $(shell xcrun --show-sdk-path)
IFLAGS  := -I$(PCAP_SDK)/usr/include

PYTHON  := .venv/bin/python3
IFACE   := en0          # change to your active interface (ifconfig to check)
PCAP_FILE :=            # set to replay a .pcap file offline

.PHONY: all sim build run offline mc send send-burst clean venv

all: build sim mc

## ── Python venv ───────────────────────────────────────────────────────
venv:
	@echo "→ Creating Python venv..."
	python3 -m venv .venv
	.venv/bin/pip install numpy scipy --quiet
	@echo "✓ venv ready"

## ── Python DPDK simulation ────────────────────────────────────────────
sim: venv
	@echo "\n→ Running DPDK Python simulation..."
	$(PYTHON) dpdk_sim.py

## ── Monte Carlo pricing ───────────────────────────────────────────────
mc: venv
	@echo "\n→ Running Monte Carlo pricing engine..."
	$(PYTHON) montecarlo_pricing.py

## ── C build ───────────────────────────────────────────────────────────
build: $(TARGET)

$(TARGET): dpdk_pcap.c
	@echo "→ Compiling dpdk_pcap.c..."
	$(CC) $(CFLAGS) $(IFLAGS) dpdk_pcap.c $(LDFLAGS) -o $(TARGET)
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

## ── Send FIX orders to en0 ────────────────────────────────────────────
send:
	$(PYTHON) send_fix_orders.py --count 20 --rate 5 --verbose

send-burst:
	$(PYTHON) send_fix_orders.py --count 1000 --rate 0

## ── Cleanup ───────────────────────────────────────────────────────────
clean:
	rm -f $(TARGET)
	find . -name "*.o" -delete
	@echo "✓ Cleaned"

## ── Help ──────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  make sim              — Python DPDK simulation"
	@echo "  make build            — compile C pcap binary"
	@echo "  make run IFACE=en0    — live capture (sudo required)"
	@echo "  make offline PCAP_FILE=x.pcap  — replay pcap file"
	@echo "  make mc               — Monte Carlo pricing"
	@echo "  make all              — build + sim + mc"
	@echo "  make send             — send 20 FIX orders to en0 at 5/s"
	@echo "  make send-burst       — send 1000 FIX orders at max rate"
	@echo "  make clean            — remove artifacts"
	@echo ""
