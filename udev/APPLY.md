# How to apply udev rules (run as root/sudo)

1. Copy the rules file to the system:
   ```
   sudo cp udev/99-kalibret-touchscreen.rules /etc/udev/rules.d/
   ```

2. Reload udev rules:
   ```
   sudo udevadm control --reload-rules
   sudo udevadm trigger --subsystem-match=input
   ```

3. Unplug and replug the touchscreen cable (or reboot)
   so the calibration matrix is applied by libinput.

To remove:
   ```
   sudo rm /etc/udev/rules.d/99-kalibret-touchscreen.rules
   sudo udevadm control --reload-rules
   sudo udevadm trigger --subsystem-match=input
   ```
