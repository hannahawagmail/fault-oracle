// SPDX-License-Identifier: Apache-2.0
/*
 * edac_cortex_ref.c — ARM Cortex-A72 Reference EDAC Driver
 *
 * A generic, poll-based EDAC driver demonstrating the standard Linux EDAC
 * framework interfaces for memory error detection and reporting on ARM platforms.
 *
 * This driver is designed to:
 *   - Run unmodified under QEMU virt (ARM64) for CI testing
 *   - Serve as a reference implementation for real Cortex-A72 hardware
 *   - Exercise all EDAC core paths (CE reporting, UE reporting, sysfs counters)
 *   - Support fault injection via debugfs for CI verification of recovery paths
 *
 * Hardware model:
 *   - 1 memory controller (mc0)
 *   - 2 chip-select rows (csrow0, csrow1) — models a 2-rank DIMM
 *   - 2 channels per csrow (ch0, ch1) — models a dual-channel configuration
 *   - ECC mode: SECDED (Single Error Correct, Double Error Detect)
 *   - Memory type: DDR4
 *   - Total modeled capacity: 4 GiB (2 csrows × 2 GiB)
 *
 * Fault injection:
 *   When CONFIG_FAULT_INJECTION=y, write to the debugfs nodes:
 *     /sys/kernel/debug/edac_cortex_ref/inject_ce   (inject correctable error)
 *     /sys/kernel/debug/edac_cortex_ref/inject_ue   (inject uncorrectable error)
 *
 * Author: Hanna Hawa <hhhawa@gmail.com>
 */

#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/edac.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/slab.h>
#include <linux/atomic.h>
#include <linux/debugfs.h>
#include <linux/uaccess.h>
#include <linux/delay.h>
#include <linux/vmalloc.h>
#include <linux/mm.h>

/* ARM RAS Extension helpers — implemented in cortex_ref_ras.c, linked as a
 * separate translation unit. Forward declarations live in cortex_ref_ras.h. */
#include "cortex_ref_ras.h"

#define DRIVER_NAME		"edac_cortex_ref"
#define DRIVER_VERSION		"1.0.0"

/* Memory topology constants */
#define CORTEX_REF_NR_CSROWS	2
#define CORTEX_REF_NR_CHANNELS	2
#define CORTEX_REF_DIMM_SIZE_MB	2048	/* 2 GiB per csrow */
#define CORTEX_REF_GRAIN	8	/* ECC symbol = 8 bytes (64-bit data + check bits) */

/* Poll interval in milliseconds (1 second default) */
#define CORTEX_REF_POLL_MSEC	1000

/*
 * Module parameters — allow runtime override without rebuild.
 */
static unsigned int poll_msec = CORTEX_REF_POLL_MSEC;
module_param(poll_msec, uint, 0644);
MODULE_PARM_DESC(poll_msec, "Error status poll interval in milliseconds (default: 1000)");

static unsigned int nr_csrows = CORTEX_REF_NR_CSROWS;
module_param(nr_csrows, uint, 0444);
MODULE_PARM_DESC(nr_csrows, "Number of chip-select rows to model (default: 2)");

static unsigned int nr_channels = CORTEX_REF_NR_CHANNELS;
module_param(nr_channels, uint, 0444);
MODULE_PARM_DESC(nr_channels, "Number of channels per csrow (default: 2)");

/*
 * irq_mode — switch between poll-driven and interrupt-driven error drain.
 *
 * When false (default), errors are drained by the EDAC core workqueue polling
 * cortex_ref_poll() every poll_msec milliseconds. This is the safest mode for
 * QEMU environments where IRQ wiring may be incomplete.
 *
 * When true, cortex_ref_poll() is a no-op and errors are drained only by the
 * cortex_ref_irq_handler() path (simulated here; on real hardware you would
 * wire this to request_irq() with the memory controller's error interrupt).
 */
static bool irq_mode = false;
module_param(irq_mode, bool, 0644);
MODULE_PARM_DESC(irq_mode, "Use interrupt-driven mode (default: false, use poll)");

/*
 * Private driver data — one instance per memory controller.
 */
struct cortex_ref_priv {
	struct mem_ctl_info	*mci;

	/*
	 * Simulated hardware error registers.
	 * On real hardware these would be MMIO addresses; here they are
	 * in-memory counters manipulated by the fault injection interface.
	 */
	atomic_t		sim_ce_count;	/* correctable errors pending */
	atomic_t		sim_ue_count;	/* uncorrectable errors pending */

	/* Fault injection target location (csrow, channel) */
	u32			inject_csrow;
	u32			inject_channel;

	/* debugfs root for this driver instance */
	struct dentry		*debugfs_dir;

	/* Statistics — cumulative counts for the poll loop */
	u64			total_ce_injected;
	u64			total_ue_injected;
	spinlock_t		stats_lock;
};

/*
 * Simulated DIMM label format: "DIMM_<csrow>_CH<channel>"
 * On real hardware this would be read from SPD or SMBIOS.
 */
static void cortex_ref_init_dimm_labels(struct mem_ctl_info *mci)
{
	int i, j;
	struct dimm_info *dimm;

	for (i = 0; i < nr_csrows; i++) {
		for (j = 0; j < nr_channels; j++) {
			dimm = edac_get_dimm(mci, i, j, 0);
			if (!dimm)
				continue;
			snprintf(dimm->label, sizeof(dimm->label),
				 "DIMM_%d_CH%d", i, j);
			dimm->grain = CORTEX_REF_GRAIN;
			dimm->dtype = DEV_X8;		/* ×8 DRAM chips — standard DDR4 */
			dimm->mtype = MEM_DDR4;
			dimm->edac_mode = EDAC_SECDED;
			dimm->nr_pages = (CORTEX_REF_DIMM_SIZE_MB << 20) >> PAGE_SHIFT;
		}
	}
}

/*
 * cortex_ref_irq_drain — drain any pending CE/UE events into the EDAC core.
 *
 * This function is the single point where atomic error counters are consumed
 * and forwarded to edac_mc_handle_error(). It is called from two places:
 *
 *   1. cortex_ref_poll()       — when irq_mode is false (default EDAC workqueue path)
 *   2. cortex_ref_irq_handler() — when irq_mode is true (simulated interrupt path)
 *
 * Centralising the drain logic avoids code duplication and ensures identical
 * EDAC core behaviour regardless of how errors are signalled.
 *
 * On real hardware the interrupt handler would clear the hardware interrupt
 * source register here and then call edac_mc_handle_error() with the actual
 * physical address and syndrome read from the memory controller's MMIO space.
 *
 * @priv: driver private data for this memory controller instance
 */
static void cortex_ref_irq_drain(struct cortex_ref_priv *priv)
{
	struct mem_ctl_info *mci = priv->mci;
	int ce_count, ue_count;
	unsigned long flags;

	/* Atomically read and clear the simulated CE counter */
	ce_count = atomic_xchg(&priv->sim_ce_count, 0);
	if (ce_count > 0) {
		spin_lock_irqsave(&priv->stats_lock, flags);
		priv->total_ce_injected += ce_count;
		spin_unlock_irqrestore(&priv->stats_lock, flags);

		/*
		 * edac_mc_handle_error() parameters:
		 *   type:        HW_EVENT_ERR_CORRECTED
		 *   mci:         this memory controller
		 *   error_count: number of errors in this report
		 *   page_frame_number: physical page (0 = unknown)
		 *   offset_in_page:    byte offset within page (0 = unknown)
		 *   syndrome:          ECC syndrome word (0 = not available)
		 *   top_layer:         csrow index
		 *   mid_layer:         channel index
		 *   low_layer:         -1 (not used for 2-layer topology)
		 *   msg:               human-readable description
		 */
		edac_mc_handle_error(
			HW_EVENT_ERR_CORRECTED,
			mci,
			ce_count,
			0,				/* page_frame_number: unknown */
			0,				/* offset_in_page: unknown */
			0,				/* syndrome: not available in simulation */
			priv->inject_csrow,
			priv->inject_channel,
			-1,
			"simulated correctable L2/DRAM ECC error"
		);

		edac_dbg(1, "Reported %d CE event(s) on csrow=%u ch=%u\n",
			 ce_count, priv->inject_csrow, priv->inject_channel);
	}

	/* Atomically read and clear the simulated UE counter */
	ue_count = atomic_xchg(&priv->sim_ue_count, 0);
	if (ue_count > 0) {
		spin_lock_irqsave(&priv->stats_lock, flags);
		priv->total_ue_injected += ue_count;
		spin_unlock_irqrestore(&priv->stats_lock, flags);

		edac_mc_handle_error(
			HW_EVENT_ERR_UNCORRECTED,
			mci,
			ue_count,
			0,				/* page_frame_number: unknown */
			0,				/* offset_in_page: unknown */
			0,				/* syndrome: not available */
			priv->inject_csrow,
			priv->inject_channel,
			-1,
			"simulated uncorrectable L2/DRAM ECC error"
		);

		edac_dbg(0, "Reported %d UE event(s) on csrow=%u ch=%u\n",
			 ue_count, priv->inject_csrow, priv->inject_channel);
	}
}

/*
 * cortex_ref_poll — called by the EDAC core workqueue every poll_msec.
 *
 * This function reads the simulated hardware error registers. On real hardware
 * it would read MMIO status registers (e.g., the L2MERRSR_EL1 register on
 * Cortex-A72, or the memory controller's ECC status registers).
 *
 * When irq_mode=true this function is a no-op; the interrupt handler is
 * responsible for draining errors. The EDAC core still calls this function
 * on its workqueue schedule, but we skip the drain to avoid double-reporting.
 *
 * Additionally, on arm64 builds, this function samples RAS Extension register
 * 0 on each poll cycle and logs the SERR field if the record is marked valid.
 * On QEMU without KVM RAS support the MRS instructions will fault; disable
 * by using a kernel with CONFIG_ARM64_RAS_EXTN=n or running under KVM.
 */
static void cortex_ref_poll(struct mem_ctl_info *mci)
{
	struct cortex_ref_priv *priv = mci->pvt_info;

	/*
	 * In interrupt-driven mode the IRQ handler calls cortex_ref_irq_drain()
	 * directly. Skip the poll drain to prevent double-counting errors.
	 */
	if (!irq_mode)
		cortex_ref_irq_drain(priv);

#ifdef CONFIG_ARM64
	/*
	 * Sample ARM RAS Extension error record 0 on each poll cycle.
	 *
	 * On real Cortex-A72/A76/N2 hardware this exposes L2 and LLC ECC
	 * errors that have already been corrected by the hardware — useful
	 * for trend analysis before a DIMM fails. In QEMU without KVM RAS
	 * support these MRS instructions will generate an Undefined Instruction
	 * exception and should not be used outside a KVM guest.
	 */
	{
		struct ras_error_record ras_rec;

		cortex_ref_read_ras_record(0, &ras_rec);
		if (ras_rec.valid) {
			pr_info(DRIVER_NAME ": RAS record[0]: valid=1 serr=0x%02x (%s)"
				" ue=%d ce=%d of=%d addr=0x%016llx\n",
				ras_rec.serr, cortex_ref_serr_name(ras_rec.serr),
				ras_rec.ue, ras_rec.ce, ras_rec.of, ras_rec.erraddr);
			/* Clear the record so hardware can capture the next error. */
			cortex_ref_clear_ras_record(0);
		}
	}
#endif /* CONFIG_ARM64 */
}

/* -------------------------------------------------------------------------
 * debugfs — fault injection interface
 * ------------------------------------------------------------------------- */

/*
 * inject_ce_write — write "N\n" to trigger N correctable error events.
 *
 * Example:
 *   echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_ce
 *   echo 5 > /sys/kernel/debug/edac_cortex_ref/inject_ce
 */
static ssize_t inject_ce_write(struct file *file, const char __user *ubuf,
			       size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	unsigned long val;
	char buf[32];
	int ret;

	if (count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;

	buf[count] = '\0';
	ret = kstrtoul(buf, 10, &val);
	if (ret)
		return ret;

	if (val == 0 || val > 1000) {
		pr_warn(DRIVER_NAME ": inject_ce: value %lu out of range [1, 1000]\n", val);
		return -ERANGE;
	}

	atomic_add((int)val, &priv->sim_ce_count);
	pr_info(DRIVER_NAME ": Queued %lu correctable error(s) for injection "
		"on csrow=%u ch=%u\n",
		val, priv->inject_csrow, priv->inject_channel);

	return count;
}

/*
 * inject_ue_write — write "N\n" to trigger N uncorrectable error events.
 */
static ssize_t inject_ue_write(struct file *file, const char __user *ubuf,
			       size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	unsigned long val;
	char buf[32];
	int ret;

	if (count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;

	buf[count] = '\0';
	ret = kstrtoul(buf, 10, &val);
	if (ret)
		return ret;

	if (val == 0 || val > 100) {
		pr_warn(DRIVER_NAME ": inject_ue: value %lu out of range [1, 100]\n", val);
		return -ERANGE;
	}

	atomic_add((int)val, &priv->sim_ue_count);
	pr_warn(DRIVER_NAME ": Queued %lu uncorrectable error(s) for injection "
		"on csrow=%u ch=%u\n",
		val, priv->inject_csrow, priv->inject_channel);

	return count;
}

/*
 * inject_csrow_write — write "N\n" to change the injection target csrow.
 */
static ssize_t inject_csrow_write(struct file *file, const char __user *ubuf,
				  size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	unsigned long val;
	char buf[32];
	int ret;

	if (count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;

	buf[count] = '\0';
	ret = kstrtoul(buf, 10, &val);
	if (ret)
		return ret;

	if (val >= nr_csrows) {
		pr_warn(DRIVER_NAME ": inject_csrow: %lu >= nr_csrows (%u)\n",
			val, nr_csrows);
		return -ERANGE;
	}

	priv->inject_csrow = (u32)val;
	return count;
}

static ssize_t inject_csrow_read(struct file *file, char __user *ubuf,
				 size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	char buf[32];
	int len;

	len = scnprintf(buf, sizeof(buf), "%u\n", priv->inject_csrow);
	return simple_read_from_buffer(ubuf, count, ppos, buf, len);
}

/*
 * inject_channel_write — write "N\n" to change the injection target channel.
 */
static ssize_t inject_channel_write(struct file *file, const char __user *ubuf,
				    size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	unsigned long val;
	char buf[32];
	int ret;

	if (count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;

	buf[count] = '\0';
	ret = kstrtoul(buf, 10, &val);
	if (ret)
		return ret;

	if (val >= nr_channels) {
		pr_warn(DRIVER_NAME ": inject_channel: %lu >= nr_channels (%u)\n",
			val, nr_channels);
		return -ERANGE;
	}

	priv->inject_channel = (u32)val;
	return count;
}

static ssize_t inject_channel_read(struct file *file, char __user *ubuf,
				   size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	char buf[32];
	int len;

	len = scnprintf(buf, sizeof(buf), "%u\n", priv->inject_channel);
	return simple_read_from_buffer(ubuf, count, ppos, buf, len);
}

/*
 * stats_read — read cumulative injected error counts.
 */
static ssize_t stats_read(struct file *file, char __user *ubuf,
			  size_t count, loff_t *ppos)
{
	struct cortex_ref_priv *priv = file->private_data;
	char buf[128];
	int len;
	unsigned long flags;
	u64 ce, ue;

	spin_lock_irqsave(&priv->stats_lock, flags);
	ce = priv->total_ce_injected;
	ue = priv->total_ue_injected;
	spin_unlock_irqrestore(&priv->stats_lock, flags);

	len = scnprintf(buf, sizeof(buf),
			"total_ce_injected: %llu\n"
			"total_ue_injected: %llu\n",
			ce, ue);

	return simple_read_from_buffer(ubuf, count, ppos, buf, len);
}

static const struct file_operations inject_ce_fops = {
	.owner		= THIS_MODULE,
	.open		= simple_open,
	.write		= inject_ce_write,
	.llseek		= default_llseek,
};

static const struct file_operations inject_ue_fops = {
	.owner		= THIS_MODULE,
	.open		= simple_open,
	.write		= inject_ue_write,
	.llseek		= default_llseek,
};

static const struct file_operations inject_csrow_fops = {
	.owner		= THIS_MODULE,
	.open		= simple_open,
	.read		= inject_csrow_read,
	.write		= inject_csrow_write,
	.llseek		= default_llseek,
};

static const struct file_operations inject_channel_fops = {
	.owner		= THIS_MODULE,
	.open		= simple_open,
	.read		= inject_channel_read,
	.write		= inject_channel_write,
	.llseek		= default_llseek,
};

static const struct file_operations stats_fops = {
	.owner		= THIS_MODULE,
	.open		= simple_open,
	.read		= stats_read,
	.llseek		= default_llseek,
};

/*
 * inject_poison_write — trigger kernel memory-failure handling on a fresh page.
 *
 * This simulates a UE that the kernel cannot correct, triggering the
 * page-offline / DIMM replacement escalation path.
 *
 * When written with a non-zero value:
 *   1. A small anonymous page is allocated via vmalloc().
 *   2. memory_failure() is called on the PFN of that page, which triggers
 *      the kernel's hardware-poison handling: the page is taken offline,
 *      mapped-out from all PTEs, and the owning process (if any) receives
 *      a SIGBUS. On systems with ACPI/APEI this would also notify firmware.
 *   3. The PFN is logged to dmesg for diagnostic purposes.
 *   4. The allocation is freed (the page is now in a bad-page list and
 *      will not be reallocated by the buddy allocator).
 *
 * Requires CONFIG_MEMORY_FAILURE=y. If not set, a diagnostic is logged and
 * -ENOSYS is returned so scripts can detect the missing configuration.
 *
 * Example:
 *   echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_poison
 *   dmesg | grep "inject_poison"
 */
static ssize_t inject_poison_write(struct file *file, const char __user *ubuf,
				   size_t count, loff_t *ppos)
{
	unsigned long val;
	char buf[32];
	int ret;

	if (count >= sizeof(buf))
		return -EINVAL;

	if (copy_from_user(buf, ubuf, count))
		return -EFAULT;

	buf[count] = '\0';
	ret = kstrtoul(buf, 10, &val);
	if (ret)
		return ret;

	if (val == 0)
		return count;	/* no-op for zero */

#ifdef CONFIG_MEMORY_FAILURE
	{
		void *addr;
		unsigned long pfn;

		/*
		 * Allocate one page of kernel virtual memory. vmalloc() is used
		 * rather than kmalloc() so that vmalloc_to_pfn() can reliably
		 * return the physical page frame number. The page is physically
		 * mapped and present in the linear map.
		 */
		addr = vmalloc(PAGE_SIZE);
		if (!addr) {
			pr_err(DRIVER_NAME ": inject_poison: vmalloc failed\n");
			return -ENOMEM;
		}

		/* Touch the page so it is fully faulted in and has a valid PFN. */
		memset(addr, 0xAB, PAGE_SIZE);

		pfn = vmalloc_to_pfn(addr);
		pr_info(DRIVER_NAME ": inject_poison: calling memory_failure() on PFN 0x%lx\n",
			pfn);

		/*
		 * memory_failure() takes the page offline, removes it from all
		 * PTEs, and sends SIGBUS to any process with a mapping. The
		 * second argument (0) means MF_ACTION_REQUIRED is not set —
		 * this is an advisory poison inject, not an MCE in-flight.
		 */
		ret = memory_failure(pfn, 0);
		if (ret)
			pr_warn(DRIVER_NAME ": inject_poison: memory_failure() returned %d "
				"(page may already be bad or offline)\n", ret);

		/*
		 * Free the vmalloc region. The underlying physical page is now
		 * on the bad-page list and will not be returned to the buddy
		 * allocator; the vmap range is still released normally.
		 */
		vfree(addr);
	}
#else
	/*
	 * CONFIG_MEMORY_FAILURE is not set — the kernel cannot take individual
	 * pages offline. Log the skip so CI scripts can detect this case and
	 * mark the test as SKIP rather than FAIL.
	 */
	pr_info(DRIVER_NAME ": inject_poison: SKIP: CONFIG_MEMORY_FAILURE not set\n");
	return -ENOSYS;
#endif /* CONFIG_MEMORY_FAILURE */

	return count;
}

static const struct file_operations inject_poison_fops = {
	.owner		= THIS_MODULE,
	.open		= simple_open,
	.write		= inject_poison_write,
	.llseek		= default_llseek,
};

static int cortex_ref_create_debugfs(struct cortex_ref_priv *priv)
{
	priv->debugfs_dir = debugfs_create_dir(DRIVER_NAME, NULL);
	if (IS_ERR_OR_NULL(priv->debugfs_dir)) {
		pr_warn(DRIVER_NAME ": failed to create debugfs directory\n");
		priv->debugfs_dir = NULL;
		return 0;  /* non-fatal — fault injection just won't work */
	}

	debugfs_create_file("inject_ce", 0200, priv->debugfs_dir, priv,
			    &inject_ce_fops);
	debugfs_create_file("inject_ue", 0200, priv->debugfs_dir, priv,
			    &inject_ue_fops);
	debugfs_create_file("inject_csrow", 0644, priv->debugfs_dir, priv,
			    &inject_csrow_fops);
	debugfs_create_file("inject_channel", 0644, priv->debugfs_dir, priv,
			    &inject_channel_fops);
	debugfs_create_file("stats", 0444, priv->debugfs_dir, priv,
			    &stats_fops);
	/*
	 * inject_poison — triggers kernel page-offline via memory_failure().
	 * See inject_poison_write() for full documentation. Write-only (0200)
	 * because the knob has no meaningful read state.
	 */
	debugfs_create_file("inject_poison", 0200, priv->debugfs_dir, priv,
			    &inject_poison_fops);

	pr_info(DRIVER_NAME ": debugfs interface at /sys/kernel/debug/%s/\n",
		DRIVER_NAME);
	return 0;
}

/* -------------------------------------------------------------------------
 * Platform driver probe / remove
 * ------------------------------------------------------------------------- */

static int cortex_ref_probe(struct platform_device *pdev)
{
	struct mem_ctl_info *mci;
	struct cortex_ref_priv *priv;
	struct edac_mc_layer layers[2];
	int ret;

	pr_info(DRIVER_NAME ": probing (nr_csrows=%u nr_channels=%u poll_msec=%u)\n",
		nr_csrows, nr_channels, poll_msec);

	/* Validate parameters */
	if (nr_csrows < 1 || nr_csrows > 16 ||
	    nr_channels < 1 || nr_channels > 8) {
		dev_err(&pdev->dev, "invalid topology parameters\n");
		return -EINVAL;
	}

	/*
	 * Define the EDAC layer topology:
	 *   Layer 0 (top):    csrow — one per DIMM rank
	 *   Layer 1 (middle): channel — one per memory channel per rank
	 *
	 * This 2D topology maps directly to the sysfs structure:
	 *   mc0/csrow<N>/ch<M>_ce_count
	 */
	layers[0].type = EDAC_MC_LAYER_CHIP_SELECT;
	layers[0].size = nr_csrows;
	layers[0].is_virt_csrow = false;

	layers[1].type = EDAC_MC_LAYER_CHANNEL;
	layers[1].size = nr_channels;
	layers[1].is_virt_csrow = false;

	/* Allocate and initialize the mem_ctl_info structure */
	mci = edac_mc_alloc(0,		/* mc_num: controller index */
			    ARRAY_SIZE(layers),
			    layers,
			    sizeof(*priv));
	if (!mci) {
		dev_err(&pdev->dev, "failed to allocate mem_ctl_info\n");
		return -ENOMEM;
	}

	priv = mci->pvt_info;
	priv->mci = mci;
	atomic_set(&priv->sim_ce_count, 0);
	atomic_set(&priv->sim_ue_count, 0);
	priv->inject_csrow   = 0;
	priv->inject_channel = 0;
	priv->total_ce_injected = 0;
	priv->total_ue_injected = 0;
	spin_lock_init(&priv->stats_lock);

	/* Fill in memory controller metadata */
	mci->mc_idx		= 0;
	mci->mtype_cap		= MEM_FLAG_DDR4;
	mci->edac_ctl_cap	= EDAC_FLAG_SECDED;
	mci->edac_cap		= EDAC_FLAG_SECDED;
	mci->mod_name		= DRIVER_NAME;
	mci->ctl_name		= "cortex-a72-l2-ecc";
	mci->dev_name		= dev_name(&pdev->dev);
	mci->edac_check		= cortex_ref_poll;
	mci->pdev		= &pdev->dev;

	/* Initialize DIMM metadata (labels, type, size) */
	cortex_ref_init_dimm_labels(mci);

	/* Register with the EDAC core */
	ret = edac_mc_add_mc(mci);
	if (ret) {
		dev_err(&pdev->dev, "edac_mc_add_mc() failed: %d\n", ret);
		edac_mc_free(mci);
		return ret;
	}

	/* Configure the EDAC core polling interval */
	edac_mc_reset_delay_period(poll_msec);

	/* Create debugfs injection interface */
	cortex_ref_create_debugfs(priv);

	platform_set_drvdata(pdev, mci);

	dev_info(&pdev->dev,
		 DRIVER_NAME " v" DRIVER_VERSION " loaded: "
		 "%u csrows × %u channels, %u MiB/csrow, poll=%u ms\n",
		 nr_csrows, nr_channels, CORTEX_REF_DIMM_SIZE_MB, poll_msec);

	return 0;
}

static int cortex_ref_remove(struct platform_device *pdev)
{
	struct mem_ctl_info *mci = platform_get_drvdata(pdev);
	struct cortex_ref_priv *priv;

	if (!mci)
		return 0;

	priv = mci->pvt_info;

	/* Remove debugfs entries */
	if (priv->debugfs_dir)
		debugfs_remove_recursive(priv->debugfs_dir);

	/* Unregister from EDAC core (stops poll timer, removes sysfs) */
	edac_mc_del_mc(mci->pdev);
	edac_mc_free(mci);

	dev_info(&pdev->dev, DRIVER_NAME ": unloaded\n");
	return 0;
}

/* -------------------------------------------------------------------------
 * Module init / exit — register a synthetic platform device
 *
 * On real hardware the platform device would be described in the Device Tree
 * (for ARM DT-based platforms) or ACPI tables (for SBSA-compliant ARM servers).
 * Here we register a synthetic platform device to allow the driver to load
 * under QEMU virt without any device tree modifications.
 * ------------------------------------------------------------------------- */

static struct platform_driver cortex_ref_driver = {
	.probe		= cortex_ref_probe,
	.remove		= cortex_ref_remove,
	.driver		= {
		.name	= DRIVER_NAME,
		.owner	= THIS_MODULE,
	},
};

static struct platform_device *cortex_ref_pdev;

static int __init cortex_ref_init(void)
{
	int ret;

	/* Register a synthetic platform device */
	cortex_ref_pdev = platform_device_register_simple(DRIVER_NAME, 0, NULL, 0);
	if (IS_ERR(cortex_ref_pdev)) {
		pr_err(DRIVER_NAME ": failed to register platform device: %ld\n",
		       PTR_ERR(cortex_ref_pdev));
		return PTR_ERR(cortex_ref_pdev);
	}

	ret = platform_driver_register(&cortex_ref_driver);
	if (ret) {
		pr_err(DRIVER_NAME ": failed to register platform driver: %d\n", ret);
		platform_device_unregister(cortex_ref_pdev);
		return ret;
	}

	pr_info(DRIVER_NAME ": module loaded (version " DRIVER_VERSION ")\n");
	return 0;
}

static void __exit cortex_ref_exit(void)
{
	platform_driver_unregister(&cortex_ref_driver);
	platform_device_unregister(cortex_ref_pdev);
	pr_info(DRIVER_NAME ": module unloaded\n");
}

module_init(cortex_ref_init);
module_exit(cortex_ref_exit);

MODULE_LICENSE("GPL v2");
MODULE_AUTHOR("Hanna Hawa <hhhawa@gmail.com>");
MODULE_DESCRIPTION("ARM Cortex-A72 Reference EDAC Driver — generic poll-based implementation");
MODULE_VERSION(DRIVER_VERSION);
MODULE_ALIAS("platform:" DRIVER_NAME);
