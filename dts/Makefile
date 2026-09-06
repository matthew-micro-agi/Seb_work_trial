# Overlays for the J722S EVM. Plain dtc, no kernel tree needed: pad values are raw k3-pinctrl encodings.
DTBOS = k3-j722s-evm-camsync-i2c2.dtbo k3-j722s-evm-csi2-imx296.dtbo k3-j722s-evm-imx296-trigger.dtbo k3-j722s-evm-camsync-pins.dtbo
all: $(DTBOS)
%.dtbo: %.dtso
	dtc -@ -q -I dts -O dtb -o $@ $<
# check against the board's base tree (fetch once: ssh evm cat /boot/dtb/ti/k3-j722s-evm.dtb > base.dtb)
check: all
	fdtoverlay -i base.dtb -o /tmp/check.dtb $(DTBOS) && echo "all overlays apply"
clean:
	rm -f *.dtbo
