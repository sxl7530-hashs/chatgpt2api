"use client";

import { Download, ImageIcon, LoaderCircle, Sparkles, Upload, Wand2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type Mode = "generate" | "edit";
type TaskStatus = "queued" | "running" | "success" | "error";

type GeneratedImage = {
  id: string;
  src: string;
};

type PublicImageJob = {
  id: string;
  status: "queued" | "running" | "success" | "error";
  result?: { data?: Array<{ b64_json?: string }> };
  error?: unknown;
};

type ReferenceImage = {
  id: string;
  file: File;
  previewUrl: string;
  originalSize: number;
  optimizedSize: number;
  width?: number;
  height?: number;
  hostUrl?: string;
  hostPath?: string;
  hostToken?: string;
  hostError?: string;
  uploadStatus: "uploading" | "success" | "error";
};

type ImageTask = {
  id: string;
  mode: Mode;
  prompt: string;
  count: number;
  size: string;
  references: ReferenceImage[];
  status: TaskStatus;
  createdAtMs: number;
  startedAtMs?: number;
  endedAtMs?: number;
  images: GeneratedImage[];
  error?: string;
};

const maxRunningTasks = 8;
const maxReferenceImageSide = 2048;
const jpegReferenceQuality = 0.94;

const sizeOptions = [
  { value: "1:1", label: "1:1", hint: "正方形" },
  { value: "16:9", label: "16:9", hint: "横屏" },
  { value: "9:16", label: "9:16", hint: "竖屏" },
  { value: "4:3", label: "4:3", hint: "横版" },
  { value: "3:4", label: "3:4", hint: "竖版" },
];

const styleOptions = [
  { value: "", label: "默认" },
  { value: "写实摄影，高级质感，细节清晰", label: "写实" },
  { value: "电影感光影，构图精致，氛围强烈", label: "电影感" },
  { value: "商业海报风格，干净背景，主体突出", label: "海报" },
  { value: "插画风格，色彩和谐，画面精致", label: "插画" },
];

function createId() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildPrompt(prompt: string, style: string, quality: boolean) {
  return [prompt.trim(), style, quality ? "高清细节，高质量，画面干净，主体清晰" : ""]
    .filter(Boolean)
    .join("，");
}

function buildEditPrompt(prompt: string) {
  return [
    "请严格以参考图为基础进行图生图，保留参考图中的主体、构图、身份特征、材质和关键视觉元素。",
    "只按照下面要求修改，不要忽略参考图。",
    prompt,
  ].join("\n");
}

function formatClock(value?: number) {
  if (!value) return "--:--:--";
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(ms: number) {
  const safeMs = Math.max(0, ms);
  const totalSeconds = Math.floor(safeMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes <= 0) return `${seconds}s`;
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s`;
}

function responseErrorMessage(payload: unknown, fallback = "生成失败") {
  if (typeof payload === "string") return payload || fallback;
  if (!payload || typeof payload !== "object") return fallback;
  const item = payload as { detail?: unknown; error?: unknown; message?: unknown };
  if (typeof item.message === "string") return item.message;
  if (typeof item.detail === "string") return item.detail;
  if (typeof item.error === "string") return item.error;
  if (item.detail && typeof item.detail === "object") return responseErrorMessage(item.detail, fallback);
  if (item.error && typeof item.error === "object") return responseErrorMessage(item.error, fallback);
  return fallback;
}

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

async function readResponse(response: Response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string, quality?: number) {
  return new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, type, quality));
}

async function optimizeReferenceImage(file: File) {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    return { file, originalSize: file.size, optimizedSize: file.size };
  }

  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, maxReferenceImageSide / Math.max(bitmap.width, bitmap.height));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { alpha: file.type !== "image/jpeg" });
    if (!context) {
      bitmap.close();
      return { file, originalSize: file.size, optimizedSize: file.size, width: bitmap.width, height: bitmap.height };
    }
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    context.drawImage(bitmap, 0, 0, width, height);
    bitmap.close();

    const outputType = file.type === "image/jpeg" ? "image/jpeg" : file.type;
    const blob = await canvasToBlob(canvas, outputType, outputType === "image/jpeg" ? jpegReferenceQuality : undefined);
    if (!blob || blob.size >= file.size * 0.98) {
      return { file, originalSize: file.size, optimizedSize: file.size, width, height };
    }
    const optimizedFile = new File([blob], file.name, { type: outputType, lastModified: Date.now() });
    return { file: optimizedFile, originalSize: file.size, optimizedSize: optimizedFile.size, width, height };
  } catch {
    return { file, originalSize: file.size, optimizedSize: file.size };
  }
}

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function waitForImageJob(taskId: string) {
  for (;;) {
    await sleep(2500);
    const response = await fetch(`/api/public/images/jobs/${taskId}`, { cache: "no-store" });
    const payload = await readResponse(response);
    if (!response.ok) {
      throw new Error(responseErrorMessage(payload, "查询任务失败"));
    }
    const job = payload as PublicImageJob;
    if (job.status === "success") return job.result || {};
    if (job.status === "error") {
      throw new Error(responseErrorMessage(job.error, "生成失败"));
    }
  }
}

function parseImageHostResponse(payload: {
  token?: string;
  path?: string;
  url?: string;
}) {
  return {
    token: String(payload.token || ""),
    path: String(payload.path || ""),
    url: String(payload.url || ""),
  };
}

export default function PublicGptImagePage() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeTaskIdsRef = useRef(new Set<string>());
  const referenceImagesRef = useRef<ReferenceImage[]>([]);

  const [mode, setMode] = useState<Mode>("generate");
  const [prompt, setPrompt] = useState("");
  const [size, setSize] = useState("1:1");
  const [count, setCount] = useState(1);
  const [style, setStyle] = useState("");
  const [quality, setQuality] = useState(true);
  const [referenceImages, setReferenceImages] = useState<ReferenceImage[]>([]);
  const [tasks, setTasks] = useState<ImageTask[]>([]);
  const [now, setNow] = useState(Date.now());

  const finalPrompt = useMemo(() => buildPrompt(prompt, style, quality), [prompt, style, quality]);
  const isEditMode = mode === "edit";
  const runningCount = tasks.filter((task) => task.status === "running").length;
  const queuedCount = tasks.filter((task) => task.status === "queued").length;

  const updateTask = (taskId: string, updates: Partial<ImageTask>) => {
    setTasks((items) => items.map((task) => (task.id === taskId ? { ...task, ...updates } : task)));
  };

  useEffect(() => {
    referenceImagesRef.current = referenceImages;
  }, [referenceImages]);

  useEffect(() => {
    if (!tasks.some((task) => task.status === "queued" || task.status === "running")) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [tasks]);

  const uploadToImageHost = async (item: ReferenceImage) => {
    const formData = new FormData();
    const token = createId().replaceAll("-", "");
    formData.append("image", item.file);
    formData.append("token", token);

    try {
      const response = await fetch("/api/public/image-host", { method: "POST", body: formData });
      const payload = await readResponse(response);
      if (!response.ok) {
        throw new Error(responseErrorMessage(payload, "图床上传失败"));
      }
      const hosted = parseImageHostResponse(payload as { token?: string; path?: string; url?: string });
      setReferenceImages((items) =>
        items.map((candidate) =>
          candidate.id === item.id
            ? { ...candidate, hostUrl: hosted.url, hostPath: hosted.path, hostToken: hosted.token, hostError: "", uploadStatus: "success" }
            : candidate,
        ),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "图床备份失败";
      setReferenceImages((items) =>
        items.map((candidate) => (candidate.id === item.id ? { ...candidate, hostError: message, uploadStatus: "error" } : candidate)),
      );
    }
  };

  const handleFiles = async (files: File[]) => {
    const imageFiles = files.filter((file) => file.type.startsWith("image/"));
    if (imageFiles.length === 0) {
      toast.error("请选择图片文件");
      return;
    }
    const optimizedFiles = await Promise.all(imageFiles.map((file) => optimizeReferenceImage(file)));
    const savedBytes = optimizedFiles.reduce((total, item) => total + Math.max(0, item.originalSize - item.optimizedSize), 0);
    const nextItems = optimizedFiles.map((item) => ({
      id: createId(),
      file: item.file,
      previewUrl: URL.createObjectURL(item.file),
      originalSize: item.originalSize,
      optimizedSize: item.optimizedSize,
      width: item.width,
      height: item.height,
      uploadStatus: "uploading" as const,
    }));
    setReferenceImages((items) => [...items, ...nextItems].slice(0, 4));
    nextItems.forEach((item) => void uploadToImageHost(item));
    if (savedBytes > 0) {
      toast.success(`参考图已优化，少传 ${formatBytes(savedBytes)}`);
    }
  };

  const removeReferenceImage = (id: string) => {
    setReferenceImages((items) => {
      const target = items.find((item) => item.id === id);
      if (target) URL.revokeObjectURL(target.previewUrl);
      return items.filter((item) => item.id !== id);
    });
  };

  const runTask = async (task: ImageTask) => {
    activeTaskIdsRef.current.add(task.id);
    updateTask(task.id, { status: "running", startedAtMs: Date.now(), error: "" });
    try {
      const response =
        task.mode === "edit"
          ? await (() => {
              const formData = new FormData();
              task.references.forEach((item) => {
                const latest = referenceImagesRef.current.find((candidate) => candidate.id === item.id) || item;
                if (latest.hostUrl) {
                  formData.append("image_url", latest.hostUrl);
                  return;
                }
                formData.append("image", latest.file);
              });
              formData.append("prompt", task.prompt);
              formData.append("n", String(task.count));
              formData.append("size", task.size);
              return fetch("/api/public/images/edits/jobs", { method: "POST", body: formData });
            })()
          : await fetch("/api/public/images/generations/jobs", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ prompt: task.prompt, n: task.count, size: task.size, response_format: "b64_json" }),
            });

      const payload = await readResponse(response);
      if (!response.ok) {
        throw new Error(responseErrorMessage(payload, "生成失败"));
      }
      const job = payload as PublicImageJob;
      if (!job.id) {
        throw new Error("任务创建失败");
      }

      const result = await waitForImageJob(job.id);
      const nextImages = ((result as { data?: Array<{ b64_json?: string }> }).data || [])
        .map((item) => item.b64_json)
        .filter((value): value is string => Boolean(value))
        .map((b64) => ({ id: createId(), src: `data:image/png;base64,${b64}` }));

      if (nextImages.length === 0) {
        throw new Error(responseErrorMessage(payload, "没有返回图片"));
      }

      updateTask(task.id, { status: "success", images: nextImages, endedAtMs: Date.now() });
      toast.success("任务完成");
    } catch (error) {
      const message = error instanceof Error ? error.message : "生成失败";
      updateTask(task.id, { status: "error", error: message, endedAtMs: Date.now() });
      toast.error(message);
    } finally {
      activeTaskIdsRef.current.delete(task.id);
    }
  };

  useEffect(() => {
    const availableSlots = maxRunningTasks - runningCount - activeTaskIdsRef.current.size;
    if (availableSlots <= 0) return;
    const nextTasks = tasks
      .filter((task) => task.status === "queued" && !activeTaskIdsRef.current.has(task.id))
      .slice(0, availableSlots);
    nextTasks.forEach((task) => void runTask(task));
  }, [tasks, runningCount]);

  const enqueueTask = () => {
    if (!prompt.trim()) {
      toast.error("请输入图片描述");
      return;
    }
    if (isEditMode && referenceImages.length === 0) {
      toast.error("请先上传参考图");
      return;
    }
    const task: ImageTask = {
      id: createId(),
      mode,
      prompt: isEditMode ? buildEditPrompt(finalPrompt) : finalPrompt,
      count,
      size,
      references: isEditMode ? [...referenceImages] : [],
      status: "queued",
      createdAtMs: Date.now(),
      images: [],
    };
    setTasks((items) => [task, ...items]);
    setPrompt("");
    toast.success("任务已加入队列，可以继续提交新的图片");
  };

  return (
    <section className="mx-auto grid h-screen w-full max-w-[1280px] grid-cols-1 gap-5 overflow-hidden py-4 lg:grid-cols-[390px_minmax(0,1fr)]">
      <div className="flex min-h-0 flex-col gap-4 overflow-hidden border-b border-stone-200 pb-5 lg:border-r lg:border-b-0 lg:pr-5 lg:pb-0">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-lg bg-stone-950 text-white">
            <Wand2 className="size-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-normal text-stone-950">xingjiabiapi.org</h1>
            <p className="text-sm text-stone-500">gpt-image-2 1k体验</p>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 rounded-lg border border-stone-200 bg-white p-1">
          {(["generate", "edit"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={cn(
                "h-10 rounded-md text-sm font-semibold transition",
                mode === value ? "bg-stone-950 text-white" : "text-stone-500 hover:bg-stone-100",
              )}
            >
              {value === "generate" ? "文生图" : "图生图"}
            </button>
          ))}
        </div>

        {isEditMode ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <label className="text-sm font-medium text-stone-700">参考图片</label>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="inline-flex h-9 items-center gap-2 rounded-lg border border-stone-200 bg-white px-3 text-sm font-medium text-stone-700 transition hover:border-stone-300"
              >
                <Upload className="size-4" />
                上传
              </button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={(event) => {
                void handleFiles(Array.from(event.target.files || []));
                event.currentTarget.value = "";
              }}
            />
            {referenceImages.length === 0 ? (
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="flex h-28 w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-stone-300 bg-white/70 text-sm text-stone-500 transition hover:border-stone-400 hover:text-stone-700"
              >
                <Upload className="size-5" />
                选择参考图
              </button>
            ) : (
              <div className="grid grid-cols-4 gap-2">
                {referenceImages.map((image) => (
                  <div key={image.id} className="relative overflow-hidden rounded-lg border border-stone-200 bg-white">
                    <img src={image.previewUrl} alt="参考图" className="aspect-square w-full object-cover" />
                    <button
                      type="button"
                      onClick={() => removeReferenceImage(image.id)}
                      className="absolute right-1 top-1 inline-flex size-6 items-center justify-center rounded-full bg-white/90 text-stone-700 shadow-sm transition hover:bg-white"
                      aria-label="移除参考图"
                    >
                      <X className="size-3.5" />
                    </button>
                    <div className="absolute inset-x-0 bottom-0 bg-black/55 px-1.5 py-1 text-center text-[10px] text-white">
                      {image.uploadStatus === "uploading" ? "图床备份中" : image.uploadStatus === "success" ? "图床已备份" : "备份失败"}
                    </div>
                    {image.optimizedSize < image.originalSize ? (
                      <div className="absolute left-1 top-1 rounded bg-white/90 px-1.5 py-0.5 text-[10px] font-medium text-stone-700 shadow-sm">
                        {formatBytes(image.optimizedSize)}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
            {referenceImages.some((image) => image.uploadStatus === "error") ? (
              <p className="text-xs leading-5 text-amber-600">
                图床备份失败不影响图生图，生成会继续使用本地上传的参考图。
              </p>
            ) : null}
          </div>
        ) : null}

        <div className="space-y-3">
          <label className="text-sm font-medium text-stone-700">图片描述</label>
          <Textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder={isEditMode ? "例如：保留人物主体，把背景改成未来城市夜景" : "例如：一张高级咖啡品牌海报，晨光从侧面照进来"}
            className="min-h-[160px] resize-none rounded-lg border-stone-200 bg-white text-[15px] leading-7 shadow-none focus-visible:ring-stone-300"
          />
        </div>

        <div className="space-y-3">
          <div className="text-sm font-medium text-stone-700">画幅比例</div>
          <div className="grid grid-cols-5 gap-2">
            {sizeOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setSize(option.value)}
                className={cn(
                  "h-16 rounded-lg border bg-white text-center transition",
                  size === option.value ? "border-stone-950 text-stone-950 shadow-sm" : "border-stone-200 text-stone-500 hover:border-stone-300",
                )}
              >
                <span className="block text-sm font-semibold">{option.label}</span>
                <span className="block text-[11px]">{option.hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">张数</label>
            <div className="grid grid-cols-4 gap-2">
              {[1, 2, 3, 4].map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setCount(value)}
                  className={cn("h-10 rounded-lg border bg-white text-sm font-semibold transition", count === value ? "border-stone-950 text-stone-950" : "border-stone-200 text-stone-500")}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">质量增强</label>
            <button
              type="button"
              onClick={() => setQuality((value) => !value)}
              className={cn("flex h-10 w-full items-center justify-center gap-2 rounded-lg border bg-white text-sm font-medium transition", quality ? "border-stone-950 text-stone-950" : "border-stone-200 text-stone-500")}
            >
              <Sparkles className="size-4" />
              {quality ? "开启" : "关闭"}
            </button>
          </div>
        </div>

        <div className="space-y-2">
          <label className="text-sm font-medium text-stone-700">风格</label>
          <div className="grid grid-cols-5 gap-2">
            {styleOptions.map((option) => (
              <button
                key={option.label}
                type="button"
                onClick={() => setStyle(option.value)}
                className={cn("h-10 rounded-lg border bg-white text-sm font-medium transition", style === option.value ? "border-stone-950 text-stone-950" : "border-stone-200 text-stone-500")}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <Button
          className="mt-auto h-12 rounded-lg bg-stone-950 text-base font-semibold text-white hover:bg-stone-800"
          onClick={enqueueTask}
          disabled={!prompt.trim() || (isEditMode && referenceImages.length === 0)}
        >
          <ImageIcon className="size-5" />
          加入生成队列
        </Button>
      </div>

      <div className="min-h-0 overflow-hidden">
        {tasks.length === 0 ? (
          <div className="flex h-full min-h-[420px] items-center justify-center rounded-lg border border-dashed border-stone-300 bg-white/60">
            <div className="text-center text-stone-500">
              <ImageIcon className="mx-auto mb-3 size-10 text-stone-300" />
              <p className="text-sm">生成任务会显示在这里</p>
            </div>
          </div>
        ) : (
          <div className="flex h-full min-h-0 flex-col gap-4">
            <div className="shrink-0 flex items-center justify-between rounded-lg border border-stone-200 bg-white px-4 py-3 text-sm text-stone-600">
              <span>运行中 {runningCount} / 排队 {queuedCount}</span>
              <span>最多同时处理 {maxRunningTasks} 个任务</span>
            </div>
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
              {tasks.map((task) => (
                <section key={task.id} className="rounded-lg border border-stone-200 bg-white p-3">
                  <div className="mb-3 flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-sm font-semibold text-stone-900">
                        <span>{task.mode === "edit" ? "图生图" : "文生图"}</span>
                        <span className="rounded bg-stone-100 px-2 py-0.5 text-xs text-stone-500">{task.size}</span>
                        <span className="rounded bg-stone-100 px-2 py-0.5 text-xs text-stone-500">{task.count} 张</span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-sm leading-6 text-stone-500">{task.prompt}</p>
                      <TaskTiming task={task} now={now} />
                    </div>
                    <TaskBadge status={task.status} />
                  </div>
                  {task.references.length > 0 ? (
                    <div className="mb-3 space-y-2">
                      <div className="flex flex-wrap gap-2">
                        {task.references.map((image) => (
                          <img key={image.id} src={image.previewUrl} alt="任务参考图" className="size-14 rounded-md object-cover" />
                        ))}
                      </div>
                      <p className="text-xs text-stone-400">
                        参考图 {task.references.length} 张，生成时优先使用图床 URL，失败时自动使用本地文件。
                      </p>
                    </div>
                  ) : null}
                  {task.status === "queued" || task.status === "running" ? (
                    <div className="flex min-h-40 items-center justify-center rounded-lg bg-stone-50 text-sm text-stone-500">
                      {task.status === "running" ? <LoaderCircle className="mr-2 size-4 animate-spin" /> : null}
                      {task.status === "running" ? "正在生成" : "等待队列"}
                    </div>
                  ) : null}
                  {task.status === "error" ? (
                    <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-700">
                      {task.error || "生成失败"}
                    </div>
                  ) : null}
                  {task.images.length > 0 ? (
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                      {task.images.map((image, index) => (
                        <figure key={image.id} className="overflow-hidden rounded-lg border border-stone-200 bg-white">
                          <button type="button" className="block w-full bg-stone-100" onClick={() => window.open(image.src, "_blank")} aria-label={`打开图片 ${index + 1}`}>
                            <img src={image.src} alt={`生成图片 ${index + 1}`} className="h-auto w-full object-contain" />
                          </button>
                          <figcaption className="flex items-center justify-between gap-3 px-3 py-3">
                            <span className="text-sm font-medium text-stone-600">图片 {index + 1}</span>
                            <a href={image.src} download={`gpt-image-${task.id}-${index + 1}.png`} className="inline-flex h-9 items-center gap-2 rounded-lg bg-stone-950 px-3 text-sm font-medium text-white transition hover:bg-stone-800">
                              <Download className="size-4" />
                              下载
                            </a>
                          </figcaption>
                        </figure>
                      ))}
                    </div>
                  ) : null}
                </section>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function TaskBadge({ status }: { status: TaskStatus }) {
  const labels: Record<TaskStatus, string> = {
    queued: "排队中",
    running: "生成中",
    success: "已完成",
    error: "失败",
  };
  return (
    <span
      className={cn(
        "shrink-0 rounded-md px-2.5 py-1 text-xs font-semibold",
        status === "success" && "bg-emerald-50 text-emerald-700",
        status === "error" && "bg-rose-50 text-rose-700",
        status === "running" && "bg-amber-50 text-amber-700",
        status === "queued" && "bg-stone-100 text-stone-500",
      )}
    >
      {labels[status]}
    </span>
  );
}

function TaskTiming({ task, now }: { task: ImageTask; now: number }) {
  const waitMs = task.startedAtMs ? task.startedAtMs - task.createdAtMs : now - task.createdAtMs;
  const runMs = task.startedAtMs ? (task.endedAtMs || now) - task.startedAtMs : 0;
  return (
    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-stone-400">
      <span>提交 {formatClock(task.createdAtMs)}</span>
      <span>开始 {formatClock(task.startedAtMs)}</span>
      <span>{task.startedAtMs ? `生成 ${formatDuration(runMs)}` : `等待 ${formatDuration(waitMs)}`}</span>
    </div>
  );
}
