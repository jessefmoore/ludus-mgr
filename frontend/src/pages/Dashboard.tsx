import { useState, useEffect, type FormEvent, type ReactNode } from "react";
import { useNavigate, Link } from "react-router-dom";
import {
  Plus,
  CalendarRange,
  PlayCircle,
  FileText,
  CheckCircle2,
  Layers,
  TrendingUp,
  TrendingDown,
  Minus,
  Server,
  Cpu,
  MemoryStick,
  HardDrive,
  Trash2,
} from "lucide-react";
import { sessions, labs, ludus, ApiError } from "@/api";
import type {
  SessionRead,
  LabTemplateRead,
  LudusRange,
  LudusCapacity,
  SessionStatus,
  LabMode,
} from "@/api";
import TopBar from "@/components/TopBar";
import Card from "@/components/Card";
import Button from "@/components/Button";
import Modal from "@/components/Modal";
import Input from "@/components/Input";
import StatusPill from "@/components/StatusPill";
import DataTable, { type Column } from "@/components/DataTable";
import { TableSkeleton } from "@/components/Skeleton";
import PageTransition from "@/components/PageTransition";
import { useToast } from "@/components/Toast";

export default function Dashboard() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [sessionList, setSessionList] = useState<SessionRead[]>([]);
  const [labList, setLabList] = useState<LabTemplateRead[]>([]);
  const [capacity, setCapacity] = useState<LudusCapacity | null>(null);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SessionRead | null>(null);
  const [destroyRanges, setDestroyRanges] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const openDelete = (s: SessionRead) => {
    setDestroyRanges(false);
    setDeleteTarget(s);
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await sessions.delete(deleteTarget.id, destroyRanges);
      toast(
        "success",
        destroyRanges
          ? `Deleted "${deleteTarget.name}" and destroyed its VMs`
          : `Deleted "${deleteTarget.name}"`,
      );
      setDeleteTarget(null);
      fetchData();
    } catch (err) {
      toast("error", err instanceof ApiError ? err.detail : "Failed to delete range");
    } finally {
      setDeleting(false);
    }
  };

  // Host capacity is best-effort: a Ludus outage shouldn't blank the dashboard.
  const fetchCapacity = () =>
    ludus.capacity().then(setCapacity).catch(() => setCapacity(null));

  const fetchData = () => {
    setLoading(true);
    Promise.all([sessions.list(), labs.list()])
      .then(([s, l]) => {
        setSessionList(s);
        setLabList(l);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
    fetchCapacity();
  };

  useEffect(fetchData, []);

  // Auto-refresh every 30s
  useEffect(() => {
    const interval = setInterval(() => {
      Promise.all([sessions.list(), labs.list()])
        .then(([s, l]) => {
          setSessionList(s);
          setLabList(l);
        })
        .catch(() => {});
      fetchCapacity();
    }, 30000);
    return () => clearInterval(interval);
  }, []);

  const sessionColumns: Column<SessionRead>[] = [
    {
      key: "name",
      label: "Name",
      sortable: true,
      sortValue: (s) => s.name.toLowerCase(),
      render: (s) => (
        <span className="text-[15px] text-text-primary font-medium">{s.name}</span>
      ),
    },
    {
      key: "lab",
      label: "Lab Template",
      render: (s) => {
        const lab = labList.find((l) => l.id === s.lab_template_id);
        return <span className="text-text-secondary">{lab?.name ?? "-"}</span>;
      },
    },
    {
      key: "mode",
      label: "Mode",
      render: (s) => (
        <span className="text-text-secondary capitalize">{s.mode}</span>
      ),
    },
    {
      key: "status",
      label: "Status",
      sortable: true,
      sortValue: (s) => s.status,
      render: (s) => <StatusPill status={s.status} />,
    },
    {
      key: "created",
      label: "Created",
      sortable: true,
      sortValue: (s) => s.created_at,
      render: (s) => (
        <span className="font-mono text-text-muted">
          {new Date(s.created_at).toLocaleDateString()}
        </span>
      ),
    },
    {
      key: "actions",
      label: "",
      render: (s) => (
        <div className="flex justify-end">
          <Button
            variant="icon"
            title="Delete range"
            aria-label="Delete range"
            onClick={(e) => {
              e.stopPropagation(); // don't trigger the row's navigate
              openDelete(s);
            }}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      ),
    },
  ];

  const count = (status: SessionStatus) =>
    sessionList.filter((s) => s.status === status).length;

  const computeTrend = (status?: SessionStatus) => {
    const now = Date.now();
    const weekMs = 7 * 24 * 60 * 60 * 1000;
    const items = status ? sessionList.filter((s) => s.status === status) : sessionList;
    const thisWeek = items.filter((s) => now - new Date(s.created_at).getTime() < weekMs).length;
    const prevWeek = items.filter((s) => {
      const age = now - new Date(s.created_at).getTime();
      return age >= weekMs && age < weekMs * 2;
    }).length;
    const delta = thisWeek - prevWeek;
    const direction: "up" | "down" | "flat" = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
    return { delta, direction };
  };

  const activeTrend = computeTrend("active");
  const totalTrend = computeTrend();

  const stats: {
    label: string;
    value: number;
    icon: typeof PlayCircle;
    accent: string;
    trend?: { delta: number; direction: "up" | "down" | "flat" };
  }[] = [
    {
      label: "Active Ranges",
      value: count("active"),
      icon: PlayCircle,
      accent: "text-accent-success",
      trend: activeTrend,
    },
    {
      label: "Draft",
      value: count("draft"),
      icon: FileText,
      accent: "text-text-secondary",
    },
    {
      label: "Total Ranges",
      value: sessionList.length,
      icon: CalendarRange,
      accent: "text-accent-info",
      trend: totalTrend,
    },
    {
      label: "Lab Templates",
      value: labList.length,
      icon: Layers,
      accent: "text-accent-info",
    },
    {
      label: "Ended",
      value: count("ended"),
      icon: CheckCircle2,
      accent: "text-text-muted",
    },
  ];

  return (
    <>
      <TopBar
        breadcrumbs={[{ label: "Dashboard" }]}
        actions={
          <Button
            variant="primary"
            icon={<Plus />}
            onClick={() => setShowCreate(true)}
          >
            New Range
          </Button>
        }
      />

      <PageTransition className="p-4 md:p-8 space-y-6">
        <div>
          <h1 className="text-2xl md:text-[32px] leading-tight font-bold text-text-primary">Ranges</h1>
          <p className="text-[15px] text-text-secondary mt-1">
            Manage your training ranges and student deployments
          </p>
        </div>

        {!loading && labList.length === 0 && (
          <Card className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 p-5 border-accent-warning/30 bg-accent-warning/5">
            <div>
              <p className="text-[15px] font-medium text-text-primary">No lab templates yet</p>
              <p className="text-sm text-text-secondary mt-1">
                Create a lab template before starting a training range.
              </p>
            </div>
            <Button variant="secondary" icon={<Layers />} onClick={() => navigate("/labs")}>
              Go to Lab Templates
            </Button>
          </Card>
        )}

        {/* Stat cards */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
          {stats.map((s) => (
            <Card key={s.label} variant="stat" className="flex items-start justify-between hover:shadow-inner-glow">
              <div>
                <p className="text-[13px] font-medium uppercase tracking-wider text-text-secondary">
                  {s.label}
                </p>
                <p className={`text-[28px] font-bold leading-none mt-2 ${s.accent}`}>
                  {s.value}
                </p>
                {s.trend && (
                  <div className={`flex items-center gap-1 mt-1.5 text-xs ${
                    s.trend.direction === "up" ? "text-accent-success" :
                    s.trend.direction === "down" ? "text-accent-danger" :
                    "text-text-muted"
                  }`}>
                    {s.trend.direction === "up" && <TrendingUp className="h-3 w-3" />}
                    {s.trend.direction === "down" && <TrendingDown className="h-3 w-3" />}
                    {s.trend.direction === "flat" && <Minus className="h-3 w-3" />}
                    <span>
                      {s.trend.direction === "up" ? "+" : ""}{s.trend.delta} this week
                    </span>
                  </div>
                )}
              </div>
              <s.icon className={`h-6 w-6 ${s.accent} opacity-60`} />
            </Card>
          ))}
        </div>

        {/* Host capacity */}
        {capacity && <HostCapacityCard capacity={capacity} />}

        {/* Ranges table */}
        <Card variant="gradient" className="p-0 overflow-hidden">
          <div className="h-1 bg-gradient-to-r from-accent-success via-accent-info/60 to-transparent" />
          <div className="px-5 py-4 border-b border-border">
            <h2 className="text-lg font-semibold text-text-primary">
              Recent Deployments
            </h2>
          </div>

          <div className="p-5">
            {loading ? (
              <TableSkeleton rows={5} cols={5} />
            ) : sessionList.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16">
                <CalendarRange className="h-12 w-12 text-text-muted mb-4" />
                <p className="text-text-secondary mb-1">No ranges yet</p>
                <p className="text-sm text-text-muted mb-6">
                  Create your first training range
                </p>
                <Button
                  variant="primary"
                  icon={<Plus />}
                  onClick={() => setShowCreate(true)}
                >
                  New Range
                </Button>
              </div>
            ) : (
              <DataTable
                columns={sessionColumns}
                data={sessionList}
                keyExtractor={(s) => s.id}
                searchable
                searchPlaceholder="Search ranges..."
                searchFilter={(s, q) => s.name.toLowerCase().includes(q)}
                onRowClick={(s) => navigate(`/sessions/${s.id}`)}
                pageSize={10}
              />
            )}
          </div>
        </Card>
      </PageTransition>

      <CreateSessionModal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        onCreated={() => {
          setShowCreate(false);
          fetchData();
        }}
        labTemplates={labList}
      />

      <Modal
        open={!!deleteTarget}
        onClose={() => !deleting && setDeleteTarget(null)}
        title="Delete Range"
        size="sm"
      >
        <p className="text-[15px] text-text-secondary mb-4">
          Delete <span className="font-medium text-text-primary">{deleteTarget?.name}</span>?
          This removes the range from the manager. By default a range that still has
          deployed VMs can't be deleted — unless you choose to destroy them below.
        </p>
        <label className="flex items-start gap-2.5 mb-6 cursor-pointer rounded-md border border-border p-3 hover:bg-bg-elevated">
          <input
            type="checkbox"
            className="mt-0.5 h-4 w-4 accent-accent-danger"
            checked={destroyRanges}
            onChange={(e) => setDestroyRanges(e.target.checked)}
          />
          <span className="text-[13px]">
            <span className="font-medium text-accent-danger">Also destroy all VMs</span>{" "}
            <span className="text-text-secondary">
              for this range. The Ludus users are kept (so their ranges can be
              reused). Permanently deletes running machines — cannot be undone.
            </span>
          </span>
        </label>
        <div className="flex justify-end gap-3">
          <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={confirmDelete} loading={deleting}>
            {destroyRanges ? "Destroy & Delete" : "Delete"}
          </Button>
        </div>
      </Modal>
    </>
  );
}

function CapacityBar({
  icon,
  label,
  allocated,
  capacity,
  available,
  unit,
  format = (n: number) => String(n),
}: {
  icon: ReactNode;
  label: string;
  allocated: number;
  capacity: number | null;
  available: number | null;
  unit: string;
  format?: (n: number) => string;
}) {
  const over = available != null && available < 0;
  // Fill fraction of allocation against capacity; capped at 100% for the bar.
  const pct = capacity && capacity > 0 ? Math.min((allocated / capacity) * 100, 100) : 0;
  const barColor = over ? "bg-accent-danger" : "bg-accent-success";
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-[13px]">
        <span className="flex items-center gap-1.5 text-text-secondary">
          {icon}
          {label}
        </span>
        {capacity == null ? (
          <span className="text-text-muted">{format(allocated)} {unit} allocated · capacity not set</span>
        ) : over ? (
          <span className="text-accent-danger font-medium">
            overcommitted by {format(Math.abs(available!))} {unit}
          </span>
        ) : (
          <span className="text-text-primary">
            <span className="font-semibold text-accent-success">{format(available!)} {unit}</span> free
          </span>
        )}
      </div>
      <div className="h-2 rounded-full bg-bg-elevated overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {capacity != null && (
        <div className="text-xs text-text-muted">
          {format(allocated)} / {format(capacity)} {unit} in use on the host
        </div>
      )}
    </div>
  );
}

const fmt1 = (n: number) => n.toFixed(n % 1 === 0 ? 0 : 1);
const fmt2 = (n: number) => n.toFixed(2);

// Proxmox reports storage in GiB; scale to TiB once it's into four digits so
// a host like "9164.8 GiB" reads as "8.95 TiB" instead.
function storageUnitAndDivisor(totalGb: number): { unit: string; divisor: number } {
  return totalGb >= 1024 ? { unit: "TiB", divisor: 1024 } : { unit: "GiB", divisor: 1 };
}

function HostCapacityCard({ capacity }: { capacity: LudusCapacity }) {
  const isProxmox = capacity.source === "proxmox";
  const { unit: storageUnit, divisor: storageDivisor } = storageUnitAndDivisor(
    capacity.storage_capacity_gb ?? 0,
  );

  return (
    <Card variant="stat" className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-text-secondary">
          <Server className="h-4 w-4" />
          <span className="text-[13px] uppercase tracking-wider font-medium">
            Host Capacity{capacity.server !== "default" ? ` · ${capacity.server}` : ""}
            {isProxmox && capacity.node ? ` · ${capacity.node}` : ""}
          </span>
        </div>
        <span className="text-xs text-text-muted">
          {capacity.range_count} live range{capacity.range_count === 1 ? "" : "s"}
        </span>
      </div>

      <CapacityBar
        icon={<Cpu className="h-3.5 w-3.5" />}
        label="CPU cores"
        allocated={capacity.cpu_allocated}
        capacity={capacity.cpu_capacity}
        available={capacity.cpu_available}
        unit="cores"
      />
      <CapacityBar
        icon={<MemoryStick className="h-3.5 w-3.5" />}
        label="RAM"
        allocated={capacity.ram_allocated_gb}
        capacity={capacity.ram_capacity_gb}
        available={capacity.ram_available_gb}
        unit="GB"
        format={fmt1}
      />
      {isProxmox && capacity.storage_capacity_gb != null && (
        <CapacityBar
          icon={<HardDrive className="h-3.5 w-3.5" />}
          label="Storage"
          allocated={(capacity.storage_used_gb ?? 0) / storageDivisor}
          capacity={capacity.storage_capacity_gb / storageDivisor}
          available={
            capacity.storage_available_gb != null
              ? capacity.storage_available_gb / storageDivisor
              : null
          }
          unit={storageUnit}
          format={fmt2}
        />
      )}

      {isProxmox && capacity.committed_cpus != null && (
        <div className="space-y-2 pt-1 border-t border-border">
          <p className="text-xs text-text-muted">
            If every VM ran at once ({capacity.vm_count} VM{capacity.vm_count === 1 ? "" : "s"}
            {" "}across {capacity.range_count} range{capacity.range_count === 1 ? "" : "s"}) -
            independent of current load above:
          </p>
          <CapacityBar
            icon={<Cpu className="h-3.5 w-3.5" />}
            label="CPU cores"
            allocated={capacity.committed_cpus}
            capacity={capacity.cpu_capacity}
            available={
              capacity.cpu_capacity != null ? capacity.cpu_capacity - capacity.committed_cpus : null
            }
            unit="cores"
          />
          <CapacityBar
            icon={<MemoryStick className="h-3.5 w-3.5" />}
            label="RAM"
            allocated={capacity.committed_ram_gb ?? 0}
            capacity={capacity.ram_capacity_gb}
            available={
              capacity.ram_capacity_gb != null && capacity.committed_ram_gb != null
                ? capacity.ram_capacity_gb - capacity.committed_ram_gb
                : null
            }
            unit="GB"
            format={fmt1}
          />
        </div>
      )}

      {!capacity.configured && (
        <p className="text-[13px] text-text-muted">
          Set PROXMOX_API_TOKEN (see README) for real host stats, or
          LUDUS_DEFAULT_CPU_CAPACITY / LUDUS_DEFAULT_RAM_CAPACITY_GB (per-server in
          Settings) for the live-range-sum fallback.
        </p>
      )}
      <p className="text-[13px] text-text-muted">
        {isProxmox
          ? "Real, measured CPU/RAM/storage usage from the Proxmox host itself (CPU shown as a core-equivalent of current load, not a reservation count)."
          : "Allocated = real CPU/RAM summed from every live range on this Ludus server (per Ludus's own data), not just ranges this app created. Set PROXMOX_API_TOKEN for real host stats instead."}
      </p>
    </Card>
  );
}

function CreateSessionModal({
  open,
  onClose,
  onCreated,
  labTemplates,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
  labTemplates: LabTemplateRead[];
}) {
  const [name, setName] = useState("");
  const [labId, setLabId] = useState<number | "">("");
  const [mode, setMode] = useState<LabMode>("shared");
  const [rangeId, setRangeId] = useState("");
  const [ownerUserid, setOwnerUserid] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [cpuQuota, setCpuQuota] = useState("");
  const [ramQuota, setRamQuota] = useState("");
  const [vmQuota, setVmQuota] = useState("");
  const [rangeQuota, setRangeQuota] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  // Range dropdown state
  const [ranges, setRanges] = useState<LudusRange[]>([]);
  const [rangesLoading, setRangesLoading] = useState(false);

  // Sync mode when lab changes
  useEffect(() => {
    if (labId !== "") {
      const lab = labTemplates.find((l) => l.id === labId);
      if (lab) setMode(lab.default_mode);
    }
  }, [labId, labTemplates]);

  // Fetch ranges when lab template changes and mode is shared
  useEffect(() => {
    if (mode !== "shared" || labId === "") {
      setRanges([]);
      return;
    }
    const lab = labTemplates.find((l) => l.id === labId);
    const server = lab?.ludus_server;
    setRangesLoading(true);
    setRanges([]);
    setRangeId("");
    ludus
      .ranges(server)
      .then((res) => setRanges(res.ranges))
      .catch(() => setRanges([]))
      .finally(() => setRangesLoading(false));
  }, [labId, mode, labTemplates]);

  const reset = () => {
    setName("");
    setLabId("");
    setMode("shared");
    setRangeId("");
    setOwnerUserid("");
    setRanges([]);
    setStartDate("");
    setEndDate("");
    setCpuQuota("");
    setRamQuota("");
    setVmQuota("");
    setRangeQuota("");
    setError("");
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (labId === "") return;
    // Owner userID only applies when auto-creating a new shared range.
    const autoCreating = !rangeId || rangeId === "__auto__";
    const owner = mode === "shared" && autoCreating ? ownerUserid.trim() : "";
    if (owner && !/^[A-Za-z0-9]{1,20}$/.test(owner)) {
      setError("Owner User ID must be 1-20 letters/numbers only (no spaces or symbols).");
      return;
    }
    setError("");
    setSaving(true);
    try {
      await sessions.create({
        name,
        lab_template_id: labId,
        mode,
        shared_range_id: mode === "shared" && rangeId && rangeId !== "__auto__" ? rangeId : null,
        owner_userid: owner || null,
        start_date: startDate ? new Date(startDate).toISOString() : null,
        end_date: endDate ? new Date(endDate).toISOString() : null,
        cpu_quota: cpuQuota ? Number(cpuQuota) : null,
        ram_quota_gb: ramQuota ? Number(ramQuota) : null,
        vm_quota: vmQuota ? Number(vmQuota) : null,
        range_quota: rangeQuota ? Number(rangeQuota) : null,
      });
      reset();
      onCreated();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail : "Failed to create range",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="New Range">
      <form onSubmit={handleSubmit} className="space-y-5">
        {error && (
          <div className="p-3 rounded-md bg-accent-danger/10 border border-accent-danger/30 text-[15px] text-accent-danger">
            {error}
          </div>
        )}

        <Input
          label="Range Name"
          placeholder="e.g. AD Attacks Workshop - April 2026"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />

        <div className="space-y-2">
          <label className="block text-[13px] uppercase tracking-wider text-text-secondary">
            Lab Template
          </label>
          {labTemplates.length === 0 ? (
            <div className="rounded-md border border-border bg-bg-elevated p-4 text-sm text-text-secondary">
              No lab templates available.{" "}
              <Link to="/labs" className="text-accent-success hover:underline">
                Create one in Lab Templates
              </Link>{" "}
              first.
            </div>
          ) : (
          <select
            className="w-full h-11 px-3 rounded-md bg-bg-elevated border border-border text-[15px] text-text-primary focus:outline-none focus:border-accent-success focus:ring-1 focus:ring-accent-success"
            value={labId}
            onChange={(e) =>
              setLabId(e.target.value ? Number(e.target.value) : "")
            }
            required
          >
            <option value="">Select a lab template...</option>
            {labTemplates.map((lab) => (
              <option key={lab.id} value={lab.id}>
                {lab.name}
              </option>
            ))}
          </select>
          )}
        </div>

        <div className="space-y-2">
          <label className="block text-[13px] uppercase tracking-wider text-text-secondary">
            Mode
          </label>
          <select
            className="w-full h-11 px-3 rounded-md bg-bg-elevated border border-border text-[15px] text-text-primary focus:outline-none focus:border-accent-success focus:ring-1 focus:ring-accent-success"
            value={mode}
            onChange={(e) => setMode(e.target.value as LabMode)}
          >
            <option value="shared">Shared</option>
            <option value="dedicated">Dedicated</option>
          </select>
        </div>

        {mode === "shared" && (
          <div className="space-y-2">
            <label className="block text-[13px] uppercase tracking-wider text-text-secondary">
              Shared Range
            </label>
            {rangesLoading ? (
              <div className="flex items-center gap-2 h-11 px-3 text-[15px] text-text-muted">
                <span className="h-4 w-4 border-2 border-text-muted/30 border-t-text-muted rounded-full animate-spin" />
                Loading ranges...
              </div>
            ) : (
              <select
                className="w-full h-11 px-3 rounded-md bg-bg-elevated border border-border text-[15px] text-text-primary focus:outline-none focus:border-accent-success focus:ring-1 focus:ring-accent-success"
                value={rangeId}
                onChange={(e) => setRangeId(e.target.value)}
              >
                <option value="">Select a range...</option>
                <option value="__auto__">Auto-create from template</option>
                {ranges.map((r) => {
                  // The chosen value becomes the session's shared_range_id
                  // (the range students are assigned access to).
                  const id = r.rangeID ?? r.userID ?? String(r.rangeNumber);
                  const label = r.name ? `${id} · ${r.name}` : id;
                  return (
                    <option key={id} value={id}>
                      {label}
                      {r.rangeState ? ` — ${r.rangeState}` : ""} (Range #{r.rangeNumber})
                    </option>
                  );
                })}
              </select>
            )}
            {(!rangeId || rangeId === "__auto__") && (
              <div className="pt-1">
                <Input
                  label="Owner User ID (optional)"
                  placeholder="blank = auto-generate"
                  value={ownerUserid}
                  onChange={(e) => setOwnerUserid(e.target.value)}
                />
                <p className="mt-1 text-xs text-text-muted">
                  When auto-creating the shared range, deploy it under this exact Ludus
                  user (1-20 letters/numbers) — e.g. to reuse a userID from a torn-down
                  range. They're enrolled as the range owner; others you add share it.
                </p>
              </div>
            )}
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          <Input
            label="Start Date"
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
          <Input
            label="End Date"
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </div>

        <div className="space-y-2">
          <label className="block text-[13px] uppercase tracking-wider text-text-secondary">
            Resource Quota <span className="text-text-muted normal-case">(optional — blank = unlimited)</span>
          </label>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <Input
              label="Max CPU cores"
              type="number"
              min={1}
              placeholder="unlimited"
              value={cpuQuota}
              onChange={(e) => setCpuQuota(e.target.value)}
            />
            <Input
              label="Max RAM (GB)"
              type="number"
              min={1}
              placeholder="unlimited"
              value={ramQuota}
              onChange={(e) => setRamQuota(e.target.value)}
            />
            <Input
              label="Max VMs"
              type="number"
              min={1}
              placeholder="unlimited"
              value={vmQuota}
              onChange={(e) => setVmQuota(e.target.value)}
            />
            <Input
              label="Max ranges"
              type="number"
              min={1}
              placeholder="unlimited"
              value={rangeQuota}
              onChange={(e) => setRangeQuota(e.target.value)}
            />
          </div>
          <p className="text-[13px] text-text-muted">
            Provisioning is blocked if the range's total demand exceeds this budget.
            {mode === "dedicated"
              ? " Dedicated mode counts one range per student."
              : " Shared mode counts a single range regardless of headcount."}
          </p>
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={saving} disabled={labTemplates.length === 0}>
            Create Range
          </Button>
        </div>
      </form>
    </Modal>
  );
}
