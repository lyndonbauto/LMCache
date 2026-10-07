
=== facts/docker.txt ===
aero-kvsink-bp: NanoCpus=0 CpuQuota=0 CpuPeriod=0 CpuShares=0 CpusetCpus='' CpusetMems='' NetworkMode=host IpcMode=private PidMode='' Privileged=false Memory=0
lmc-c: NanoCpus=0 CpuQuota=0 CpuPeriod=0 CpuShares=0 CpusetCpus='' CpusetMems='' NetworkMode=host IpcMode=host PidMode='' Privileged=false Memory=0

=== facts/machine.txt ===
## uname -r
6.8.0-138-generic
## /proc/cmdline
BOOT_IMAGE=/vmlinuz-6.8.0-138-generic root=UUID=cc6f3ac6-f24d-4276-abe9-c242adec4e04 ro cgroup_enable=memory swapaccount=1 console=tty1 console=ttyS0 net.ifnames=0 biosdevname=0
## lscpu
Architecture:                            x86_64
CPU op-mode(s):                          32-bit, 64-bit
Address sizes:                           46 bits physical, 57 bits virtual
Byte Order:                              Little Endian
CPU(s):                                  20
On-line CPU(s) list:                     0-19
Vendor ID:                               GenuineIntel
BIOS Vendor ID:                          QEMU
Model name:                              INTEL(R) XEON(R) PLATINUM 8568Y+
BIOS Model name:                         pc-q35-6.1  CPU @ 2.0GHz
BIOS CPU family:                         1
CPU family:                              6
Model:                                   207
Thread(s) per core:                      1
Core(s) per socket:                      20
Socket(s):                               1
Stepping:                                2
BogoMIPS:                                4600.00
Flags:                                   fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts mmx fxsr sse sse2 ss ht syscall nx pdpe1gb rdtscp lm constant_tsc arch_perfmon pebs bts rep_good nopl xtopology cpuid tsc_known_freq pni pclmulqdq dtes64 vmx ssse3 fma cx16 pdcm pcid sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand hypervisor lahf_lm abm 3dnowprefetch cpuid_fault ssbd ibrs ibpb stibp ibrs_enhanced tpr_shadow flexpriority ept vpid ept_ad fsgsbase tsc_adjust bmi1 avx2 smep bmi2 erms invpcid avx512f avx512dq rdseed adx smap avx512ifma clflushopt clwb avx512cd sha_ni avx512bw avx512vl xsaveopt xsavec xgetbv1 xsaves avx_vnni avx512_bf16 wbnoinvd arat vnmi avx512vbmi umip pku ospke waitpkg avx512_vbmi2 gfni vaes vpclmulqdq avx512_vnni avx512_bitalg avx512_vpopcntdq la57 rdpid bus_lock_detect cldemote movdiri movdir64b overflow_recov succor fsrm md_clear serialize tsxldtrk avx512_fp16 arch_capabilities
Virtualization:                          VT-x
Hypervisor vendor:                       KVM
Virtualization type:                     full
L1d cache:                               640 KiB (20 instances)
L1i cache:                               640 KiB (20 instances)
L2 cache:                                80 MiB (20 instances)
NUMA node(s):                            1
NUMA node0 CPU(s):                       0-19
Vulnerability Gather data sampling:      Not affected
Vulnerability Indirect target selection: Mitigation; Aligned branch/return thunks
Vulnerability Itlb multihit:             Not affected
Vulnerability L1tf:                      Not affected
Vulnerability Mds:                       Not affected
Vulnerability Meltdown:                  Not affected
Vulnerability Mmio stale data:           Unknown: No mitigations
Vulnerability Reg file data sampling:    Not affected
Vulnerability Retbleed:                  Not affected
Vulnerability Spec rstack overflow:      Not affected
Vulnerability Spec store bypass:         Mitigation; Speculative Store Bypass disabled via prctl
Vulnerability Spectre v1:                Mitigation; usercopy/swapgs barriers and __user pointer sanitization
Vulnerability Spectre v2:                Mitigation; Enhanced / Automatic IBRS; IBPB conditional; PBRSB-eIBRS SW sequence; BHI SW loop, KVM SW loop
Vulnerability Srbds:                     Not affected
Vulnerability Tsa:                       Not affected
Vulnerability Tsx async abort:           Mitigation; TSX disabled
Vulnerability Vmscape:                   Not affected
## rdma_rxe (loaded)
srcversion 1AE0DE565BFD276050040E6
file srcversion 1AE0DE565BFD276050040E6
file /root/rxe-build/v6.11-20261007/rdma_rxe.ko (upstream v6.11 rxe built for MLNX OFED 24.10, functional/HOST-CHANGES.md)
## workqueue cpumask
fffff
/sys/devices/virtual/workqueue/blkcg_punt_bio/cpumask fffff
/sys/devices/virtual/workqueue/ib-comp-unb-wq/cpumask fffff
/sys/devices/virtual/workqueue/raid5wq/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_0/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_1/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_2/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_3/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_4/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_5/cpumask fffff
/sys/devices/virtual/workqueue/scsi_tmf_6/cpumask fffff
/sys/devices/virtual/workqueue/writeback/cpumask fffff
## isolcpus / nohz_full

              (null)
## cpupower
analyzing CPU 3:
  no or unknown cpufreq driver is active on this CPU
  CPUs which run at the same hardware frequency: Not Available
  CPUs which need to have their frequency coordinated by software: Not Available
  maximum transition latency:  Cannot determine or is not supported.
Not Available
  available cpufreq governors: Not Available
  Unable to determine current policy
  current CPU frequency: Unable to call hardware
  current CPU frequency:  Unable to call to kernel
  boost state support:
    Supported: no
    Active: no
CPUidle driver: none
CPUidle governor: menu
analyzing CPU 18:

CPU 18: No idle states

## cpufreq (cpu0)
scaling_driver cat: /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver: No such file or directory
scaling_governor cat: /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor: No such file or directory
cpuinfo_min_freq cat: /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq: No such file or directory
cpuinfo_max_freq cat: /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq: No such file or directory
scaling_cur_freq cat: /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq: No such file or directory
## cpuidle driver
none
menu
menu
## cpuidle states (cpu0): name latency_us residency_us usage time_us disable
cat: '/sys/devices/system/cpu/cpu0/cpuidle/state*/name': No such file or directory
cat: '/sys/devices/system/cpu/cpu0/cpuidle/state*/latency': No such file or directory
cat: '/sys/devices/system/cpu/cpu0/cpuidle/state*/residency': No such file or directory
cat: '/sys/devices/system/cpu/cpu0/cpuidle/state*/usage': No such file or directory
cat: '/sys/devices/system/cpu/cpu0/cpuidle/state*/time': No such file or directory
cat: '/sys/devices/system/cpu/cpu0/cpuidle/state*/disable': No such file or directory
state*      
## free -g
               total        used        free      shared  buff/cache   available
Mem:             235           6         229           0           2         229
Swap:              0           0           0
## numactl
available: 1 nodes (0)
node 0 cpus: 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19
node 0 size: 241610 MB
node 0 free: 234616 MB
node distances:
## irqbalance
inactive
## steal since boot (/proc/stat cpu line)
cpu  2570 1197 2350 1963659 423 0 45 2 0 0

=== ib_write_bw -s 524288 -q 16 -t 2, 10 s (reference) ===
102.56 Gb/s = 11.94 GiB/s

=== ib_write_bw mpstat (5 s) ===
CPUs busy: 16.9

=== (a) vLLM + LMCache MP server up and idle ===
kvlayers --qps 16 --duration 20: 7.95 GiB/s, failed rows 0
vLLM/LMCache processes in lmc-c during the stream: 2
asd nodes: 1 (single-node mesh config); stats lines from that node:
  16786 writes 8.19 GiB/s failed 0 | us/write queued 14926 placer-wait 1065 read 2 copy 62 post 0 wire 6480 reply 0 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 17.1, starved 0%
  16690 writes 8.15 GiB/s failed 0 | us/write queued 36852 placer-wait 909 read 2 copy 56 post 0 wire 6726 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 15.9 of 16.0, placer queue 12.5, starved 0%

mpstat -P ALL 1 10, averages:
 CPU    %usr    %sys    %irq   %soft  %steal   %idle
 all    4.22   44.07    0.00    0.78    0.00   50.93
   0    4.10   42.99    0.00    6.39    0.00   46.52
   1    4.50   43.84    0.00    3.62    0.00   48.04
   2    3.78   43.88    0.00    1.89    0.00   50.45
   3    4.20   45.30    0.00    1.20    0.00   49.30
   4    4.44   44.86    0.00    0.71    0.00   50.00
   5    4.63   44.51    0.00    0.50    0.00   50.35
   6    3.73   41.37    0.00    0.40    0.00   54.49
   7    3.94   42.87    0.00    0.20    0.00   52.98
   8    3.73   43.65    0.00    0.10    0.00   52.52
   9    3.64   41.80    0.00    0.10    0.00   54.45
  10    4.45   44.84    0.00    0.00    0.00   50.71
  11    3.94   43.48    0.00    0.00    0.00   52.58
  12    4.64   45.56    0.00    0.00    0.00   49.80
  13    4.33   43.81    0.00    0.10    0.00   51.76
  14    4.67   45.23    0.00    0.00    0.00   50.10
  15    4.04   44.08    0.00    0.00    0.00   51.87
  16    4.34   44.85    0.00    0.00    0.00   50.81
  17    4.35   44.94    0.00    0.00    0.00   50.71
  18    4.46   45.09    0.00    0.00    0.00   50.46
  19    4.46   44.42    0.00    0.00    0.00   51.12
CPUs busy (sum of 1 - %idle): 9.8

top -b -H, 25 threads (second frame, 2 s interval, 6 s into the stream):
    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
  10819 root      20   0       0      0      0 I  14.3   0.0   0:06.41 kworker/u49:5-rxe_wq
  15938 root      20   0       0      0      0 I  14.3   0.0   0:00.48 kworker/u46:5-rxe_wq
    273 root      20   0       0      0      0 I  13.8   0.0   0:06.24 kworker/u60:1-rxe_wq
  15931 root      20   0       0      0      0 R  13.3   0.0   0:02.90 kworker/u44:4-rxe_wq
  15953 root      20   0       0      0      0 I  13.3   0.0   0:03.46 kworker/u60:0-rxe_wq
  15970 root      20   0       0      0      0 I  13.3   0.0   0:02.40 kworker/u48:6-rxe_wq
  16225 root      20   0   67.0g   1.7g  17864 S  13.3   0.7   0:05.92 asd
   4062 root      20   0       0      0      0 I  12.8   0.0   0:03.32 kworker/u52:2-rxe_wq
  10804 root      20   0       0      0      0 I  12.8   0.0   0:04.10 kworker/u58:5-rxe_wq
  17404 root      20   0       0      0      0 R  12.8   0.0   0:00.70 kworker/u42:5-rxe_wq
  15958 root      20   0       0      0      0 I  12.3   0.0   0:03.23 kworker/u59:5-rxe_wq
  15978 root      20   0       0      0      0 I  12.3   0.0   0:00.78 kworker/u43:4-rxe_wq
  17407 root      20   0       0      0      0 R  12.3   0.0   0:00.56 kworker/u41:3-rxe_wq
    423 root      20   0       0      0      0 R  11.8   0.0   0:02.06 kworker/u57:5+rxe_wq
   1325 root      20   0       0      0      0 I  11.8   0.0   0:03.20 kworker/u55:2-rxe_wq
  10830 root      20   0       0      0      0 I  11.8   0.0   0:04.83 kworker/u42:3-rxe_wq
  15952 root      20   0       0      0      0 I  11.8   0.0   0:03.64 kworker/u59:2-rxe_wq
  10797 root      20   0       0      0      0 I  11.3   0.0   0:06.21 kworker/u53:3-rxe_wq
  15949 root      20   0       0      0      0 I  11.3   0.0   0:00.65 kworker/u50:5-rxe_wq
  15957 root      20   0       0      0      0 I  11.3   0.0   0:00.54 kworker/u59:3-rxe_wq
    140 root      20   0       0      0      0 I  10.8   0.0   0:05.01 kworker/u46:0-rxe_wq
    183 root      20   0       0      0      0 I  10.8   0.0   0:06.05 kworker/u43:1-rxe_wq
    602 root      20   0       0      0      0 I  10.8   0.0   0:04.05 kworker/u48:2-rxe_wq
   2958 root      20   0       0      0      0 I  10.8   0.0   0:05.42 kworker/u54:2-rxe_wq
   4579 root      20   0       0      0      0 R  10.8   0.0   0:03.76 kworker/u44:2+rxe_wq

/proc/interrupts delta over the 20 s stream, top 10 sources:
source        delta  description                              CPUs (share of this source)
LOC          324917  Local timer interrupts                   cpu17 7%, cpu1 5%, cpu14 5%, cpu0 5%, cpu18 5%, cpu16 5%, +14 more CPUs
CAL          227087  Function call interrupts                 cpu0 8%, cpu1 7%, cpu2 6%, cpu3 5%, cpu16 5%, cpu14 5%, +14 more CPUs
RES            5132  Rescheduling interrupts                  cpu9 6%, cpu6 6%, cpu7 6%, cpu11 6%, cpu10 6%, cpu18 5%, +14 more CPUs
TLB             949  TLB shootdowns                           cpu16 11%, cpu5 8%, cpu11 7%, cpu17 6%, cpu3 6%, cpu13 6%, +14 more CPUs
160             100  PCI-MSIX-0000:00:01.0 1-edge virtio0-con cpu7 100%
51               14  PCI-MSIX-0000:05:00.0 17-edge virtio4-re cpu16 100%
38                6  PCI-MSIX-0000:05:00.0 4-edge virtio4-req cpu3 100%
45                5  PCI-MSIX-0000:05:00.0 11-edge virtio4-re cpu10 100%
116               5  PCI-MSIX-0000:01:00.0 15-edge virtio1-in cpu14 100%
49                4  PCI-MSIX-0000:05:00.0 15-edge virtio4-re cpu14 100%

=== (b) vLLM and LMCache MP server stopped ===
kvlayers --qps 16 --duration 20: 7.83 GiB/s, failed rows 0
vLLM/LMCache processes in lmc-c during the stream: 0
asd nodes: 1 (single-node mesh config); stats lines from that node:
  17019 writes 8.30 GiB/s failed 0 | us/write queued 26749 placer-wait 1198 read 1 copy 52 post 0 wire 6317 reply 0 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 17.6, starved 0%
  16375 writes 7.99 GiB/s failed 0 | us/write queued 24094 placer-wait 1051 read 1 copy 51 post 0 wire 6714 reply 1 | in flight 128.0 writes 64.0 MiB, busy qps 16.0 of 16.0, placer queue 14.6, starved 0%

mpstat -P ALL 1 10, averages:
 CPU    %usr    %sys    %irq   %soft  %steal   %idle
 all    3.96   43.34    0.00    0.56    0.00   52.14
   0    3.57   40.25    0.00    5.02    0.00   51.16
   1    4.15   42.59    0.00    2.37    0.00   50.89
   2    4.19   42.87    0.00    1.50    0.00   51.45
   3    3.93   43.25    0.00    0.71    0.00   52.12
   4    3.93   43.61    0.00    0.50    0.00   51.96
   5    3.75   45.80    0.00    0.20    0.00   50.25
   6    3.94   43.74    0.00    0.20    0.00   52.12
   7    4.13   46.17    0.00    0.10    0.00   49.60
   8    3.44   41.15    0.00    0.10    0.00   55.31
   9    3.75   42.05    0.00    0.00    0.00   54.20
  10    4.37   41.83    0.00    0.10    0.00   53.71
  11    4.45   43.88    0.00    0.10    0.00   51.57
  12    3.64   42.91    0.00    0.00    0.00   53.44
  13    3.95   41.24    0.00    0.00    0.00   54.81
  14    3.96   42.49    0.00    0.00    0.00   53.55
  15    4.15   47.01    0.00    0.00    0.00   48.83
  16    3.05   41.67    0.00    0.00    0.00   55.28
  17    4.15   44.88    0.00    0.00    0.00   50.96
  18    4.26   47.01    0.00    0.00    0.00   48.73
  19    4.35   42.61    0.00    0.00    0.00   53.04
CPUs busy (sum of 1 - %idle): 9.6

top -b -H, 25 threads (second frame, 2 s interval, 6 s into the stream):
    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
  15936 root      20   0       0      0      0 I  14.9   0.0   0:01.84 kworker/u46:2-rxe_wq
  15947 root      20   0       0      0      0 I  12.9   0.0   0:02.68 kworker/u52:0-rxe_wq
  15960 root      20   0       0      0      0 I  12.9   0.0   0:02.01 kworker/u60:2-rxe_wq
    145 root      20   0       0      0      0 I  12.4   0.0   0:07.70 kworker/u51:0-rxe_wq
    706 root      20   0       0      0      0 R  12.4   0.0   0:04.32 kworker/u56:1+rxe_wq
   4579 root      20   0       0      0      0 I  12.4   0.0   0:05.61 kworker/u44:2-rxe_wq
   1358 root      20   0       0      0      0 I  11.9   0.0   0:05.95 kworker/u58:1-rxe_wq
  10816 root      20   0       0      0      0 I  11.9   0.0   0:06.60 kworker/u50:0-rxe_wq
  18081 root      20   0   67.0g   1.6g  17780 S  11.9   0.7   0:01.78 asd
  10823 root      20   0       0      0      0 I  11.4   0.0   0:07.67 kworker/u53:5-rxe_wq
  10845 root      20   0       0      0      0 I  11.4   0.0   0:03.27 kworker/u43:6-rxe_wq
  17419 root      20   0       0      0      0 I  11.4   0.0   0:01.22 kworker/u46:6-rxe_wq
    135 root      20   0       0      0      0 I  10.9   0.0   0:05.87 kworker/u41:0-rxe_wq
    142 root      20   0       0      0      0 I  10.9   0.0   0:04.09 kworker/u48:0-rxe_wq
    150 root      20   0       0      0      0 I  10.9   0.0   0:04.62 kworker/u56:0-rxe_wq
   1252 root      20   0       0      0      0 I  10.9   0.0   0:04.29 kworker/u42:2-rxe_wq
  15956 root      20   0       0      0      0 I  10.9   0.0   0:04.05 kworker/u45:2-rxe_wq
  17408 root      20   0       0      0      0 I  10.9   0.0   0:02.22 kworker/u47:5-rxe_wq
   1325 root      20   0       0      0      0 I  10.4   0.0   0:04.96 kworker/u55:2-rxe_wq
  10796 root      20   0       0      0      0 I  10.4   0.0   0:05.66 kworker/u59:0-rxe_wq
  15922 root      20   0       0      0      0 I  10.4   0.0   0:04.85 kworker/u47:0-rxe_wq
  15929 root      20   0       0      0      0 I  10.4   0.0   0:04.81 kworker/u49:0-rxe_wq
  15943 root      20   0       0      0      0 I  10.4   0.0   0:02.73 kworker/u53:0-rxe_wq
  15955 root      20   0       0      0      0 I  10.4   0.0   0:04.85 kworker/u41:2-rxe_wq
  15974 root      20   0       0      0      0 I  10.4   0.0   0:02.46 kworker/u58:4-rxe_wq

/proc/interrupts delta over the 20 s stream, top 10 sources:
source        delta  description                              CPUs (share of this source)
LOC          301300  Local timer interrupts                   cpu1 7%, cpu18 5%, cpu5 5%, cpu10 5%, cpu3 5%, cpu19 5%, +14 more CPUs
CAL          212705  Function call interrupts                 cpu0 8%, cpu1 6%, cpu2 6%, cpu3 5%, cpu6 5%, cpu4 5%, +14 more CPUs
RES            5414  Rescheduling interrupts                  cpu7 7%, cpu16 6%, cpu13 6%, cpu8 6%, cpu12 5%, cpu19 5%, +14 more CPUs
TLB            1144  TLB shootdowns                           cpu19 9%, cpu3 8%, cpu11 8%, cpu6 7%, cpu1 7%, cpu4 6%, +14 more CPUs
160             101  PCI-MSIX-0000:00:01.0 1-edge virtio0-con cpu7 100%
51                7  PCI-MSIX-0000:05:00.0 17-edge virtio4-re cpu16 100%
163               4  PCI-MSIX-0000:83:00.0 0-edge amdgpu      cpu10 100%
40                3  PCI-MSIX-0000:05:00.0 6-edge virtio4-req cpu5 100%
109               3  PCI-MSIX-0000:01:00.0 8-edge virtio1-out cpu7 100%
108               2  PCI-MSIX-0000:01:00.0 7-edge virtio1-inp cpu6 100%
