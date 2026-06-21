# PCIe AER Error Taxonomy

PCIe AER (Advanced Error Reporting) classifies errors into three categories based
on severity. Each category maps to a distinct set of status register bits.

## Correctable Errors

Correctable errors are automatically recovered by the PCIe link.
Source: PCIe Base Spec §6.2.3.2, AER Extended Capability registers.

| Bit | Error Name | sysfs file | Description |
|-----|-----------|-----------|-------------|
| 0   | Receiver Error | `aer_dev_correctable` | Receiver detected an 8b/10b or 128b/130b decode error |
| 6   | Bad TLP | `aer_dev_correctable` | Malformed TLP received (bad sequence number or LCRC) |
| 7   | Bad DLLP | `aer_dev_correctable` | Bad DLLP (bad CRC on data link layer) |
| 8   | REPLAY_NUM Rollover | `aer_dev_correctable` | Replay buffer overflow counter |
| 12  | Replay Timer Timeout | `aer_dev_correctable` | Replay timer expired before ACK received |
| 13  | Advisory Non-Fatal | `aer_dev_correctable` | Flow control protocol error (advisory) |
| 14  | Corrected Internal Error | `aer_dev_correctable` | Internal error corrected by device |
| 15  | Header Log Overflow | `aer_dev_correctable` | Header log register overflow |

## Uncorrectable Non-Fatal Errors

Recoverable but not automatically corrected. The AER service driver handles recovery.

| Bit | Error Name | Description |
|-----|-----------|-------------|
| 4   | Data Link Protocol Error | DLLP sequence number error |
| 5   | Surprise Down | Link went down unexpectedly |
| 12  | Poisoned TLP | TLP arrived with EP bit set (poisoned data) |
| 13  | Flow Control Protocol Error | FC credits violated |
| 14  | Completion Timeout | Requester did not receive completion |
| 15  | Completer Abort | Completer returned CA status |
| 16  | Unexpected Completion | Completion arrived for no outstanding request |
| 17  | Receiver Overflow | Receiver buffer overflow |
| 18  | Malformed TLP | TLP structurally invalid |
| 19  | ECRC Error | End-to-end CRC mismatch |
| 20  | Unsupported Request | Completer does not support the request |
| 21  | ACS Violation | Access Control Services violation |
| 22  | Uncorrectable Internal Error | Internal device error, non-fatal |
| 23  | MC Blocked TLP | TLP blocked by multicast service |
| 24  | AtomicOp Egress Blocked | AtomicOp blocked on egress port |
| 25  | TLP Prefix Blocked | TLP prefix not supported |

## Uncorrectable Fatal Errors

Fatal errors cause the PCIe link or the entire device to become unreliable.
Recovery requires a device reset (hot reset or Function Level Reset).

| Bit | Error Name | Description |
|-----|-----------|-------------|
| 4   | Data Link Protocol Error (fatal) | Same as non-fatal but marked fatal by device |
| 5   | Surprise Down (fatal) | Unrecoverable link-down event |
| 12  | Poisoned TLP Received (fatal) | Received poisoned TLP in fatal context |
| 18  | Malformed TLP (fatal) | Unrecoverable TLP format violation |

## sysfs Interface

Each PCI device with AER support exposes:

```
/sys/bus/pci/devices/<BDF>/
  aer_dev_correctable       — correctable error counts (space-separated name=count pairs)
  aer_dev_nonfatal          — non-fatal uncorrectable error counts
  aer_dev_fatal             — fatal error counts
```

Example:
```
$ cat /sys/bus/pci/devices/0000:01:00.0/aer_dev_correctable
RxErr 0 BadTLP 0 BadDLLP 2 Rollover 0 Timeout 0 NonFatalErr 0 CorrIntErr 0 HeaderOF 0 TOTAL_ERR_COR 2
```

## Linux Kernel References

- `drivers/pci/pcie/aer.c` — AER service driver, error recovery logic
- `include/linux/aer.h` — AER error bit definitions
- `Documentation/PCI/pcieaer-howto.rst` — kernel documentation
