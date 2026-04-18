import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";

// §14 — avatar editor modal opened from the /me avatar. The user picks
// an image (file picker, or front camera on mobile via `capture=user`),
// pans via drag and zooms via the range slider, then Save renders the
// circular viewport to a 512x512 WebP via canvas and POSTs it multipart
// to /api/v1/me/avatar. Per-user avatars are self-only (§05, §12); a
// "Remove photo" button on the same modal calls DELETE.
//
// The on-screen stage is responsive (`min(320px, 86vw)`) so the modal
// fits on narrow phones. Pan values live in *stage-pixel* coordinates
// and we measure the stage at render time rather than assuming a fixed
// 256px viewport; crop math then works at any width. Below 640px the
// modal becomes a full-height sheet (see `.modal--sheet` in globals).

const OUT = 512; // output image dimensions

interface Props {
  open: boolean;
  onClose: () => void;
  currentUrl: string | null;
  userName: string;
}

export default function AvatarEditor({ open, onClose, currentUrl, userName }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [error, setError] = useState<string | null>(null);
  const drag = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null);

  useEffect(() => {
    if (open) ref.current?.showModal();
    else ref.current?.close();
  }, [open]);

  useEffect(() => {
    return () => {
      if (imgUrl) URL.revokeObjectURL(imgUrl);
    };
  }, [imgUrl]);

  const reset = () => {
    if (imgUrl) URL.revokeObjectURL(imgUrl);
    setFile(null);
    setImgUrl(null);
    setNatural(null);
    setScale(1);
    setPan({ x: 0, y: 0 });
    setError(null);
  };

  const stageSize = (): number => {
    const rect = stageRef.current?.getBoundingClientRect();
    return rect && rect.width > 0 ? rect.width : 256;
  };

  const onFile = (f: File | null) => {
    if (!f) return;
    if (!f.type.startsWith("image/")) {
      setError("Pick an image file.");
      return;
    }
    const url = URL.createObjectURL(f);
    const img = new Image();
    img.onload = () => setNatural({ w: img.naturalWidth, h: img.naturalHeight });
    img.onerror = () => setError("Couldn't read that image.");
    img.src = url;
    setFile(f);
    setImgUrl(url);
    setError(null);
  };

  // Once the stage has mounted and the image has loaded, pick a
  // starting scale that just covers the circular viewport.
  useEffect(() => {
    if (!natural || !stageRef.current) return;
    const view = stageSize();
    setScale(view / Math.min(natural.w, natural.h));
    setPan({ x: 0, y: 0 });
  }, [natural]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!natural) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    drag.current = { startX: e.clientX, startY: e.clientY, origX: pan.x, origY: pan.y };
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current || !natural) return;
    setPan({
      x: drag.current.origX + (e.clientX - drag.current.startX),
      y: drag.current.origY + (e.clientY - drag.current.startY),
    });
  };
  const onPointerUp = () => {
    drag.current = null;
  };

  const save = useMutation({
    mutationFn: async () => {
      if (!file || !natural) throw new Error("no_image");
      const view = stageSize();
      // Compose the cropped 512x512 output on an offscreen canvas.
      const canvas = document.createElement("canvas");
      canvas.width = OUT;
      canvas.height = OUT;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("canvas_unavailable");
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, OUT, OUT);
      const img = new Image();
      img.src = imgUrl!;
      await img.decode();
      const scaled = { w: natural.w * scale, h: natural.h * scale };
      // In stage-pixel coords the image top-left sits at
      //   tl = (view - scaled)/2 + pan.
      const tlX = (view - scaled.w) / 2 + pan.x;
      const tlY = (view - scaled.h) / 2 + pan.y;
      const ratio = OUT / view;
      ctx.drawImage(img, tlX * ratio, tlY * ratio, scaled.w * ratio, scaled.h * ratio);
      const blob: Blob = await new Promise((resolve, reject) => {
        canvas.toBlob(
          (b) => (b ? resolve(b) : reject(new Error("encode_failed"))),
          "image/webp",
          0.9,
        );
      });
      const form = new FormData();
      form.append("image", new File([blob], "avatar.webp", { type: "image/webp" }));
      // Also forward the crop box as source-pixel coords so the server
      // can re-crop authoritatively (§12).
      form.append("crop_x", String(Math.round(-pan.x / scale + natural.w / 2 - view / (2 * scale))));
      form.append("crop_y", String(Math.round(-pan.y / scale + natural.h / 2 - view / (2 * scale))));
      form.append("crop_size", String(Math.round(view / scale)));
      return fetchJson<{ user: unknown; avatar_url: string | null }>(
        "/api/v1/me/avatar",
        { method: "POST", body: form },
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.me() });
      qc.invalidateQueries({ queryKey: qk.employees() });
      reset();
      onClose();
    },
    onError: (e) => setError(e instanceof Error ? e.message : "upload_failed"),
  });

  const remove = useMutation({
    mutationFn: () =>
      fetchJson<{ avatar_url: string | null }>("/api/v1/me/avatar", { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.me() });
      qc.invalidateQueries({ queryKey: qk.employees() });
      onClose();
    },
  });

  const currentView = stageRef.current?.getBoundingClientRect().width ?? 256;
  const minScale = natural ? currentView / Math.min(natural.w, natural.h) : 0.2;
  const maxScale = natural ? minScale * 4 : 2;

  const bgStyle = natural && imgUrl
    ? {
        backgroundImage: `url(${imgUrl})`,
        backgroundSize: `${natural.w * scale}px ${natural.h * scale}px`,
        backgroundPosition: `calc(50% + ${pan.x}px) calc(50% + ${pan.y}px)`,
      }
    : undefined;

  return (
    <dialog className="modal modal--sheet" ref={ref} onClose={() => { reset(); onClose(); }}>
      <div className="modal__body avatar-editor">
        <h3 className="modal__title">Profile photo</h3>
        <p className="modal__sub">
          {natural
            ? "Drag to reposition, slide to zoom."
            : "Pick an image — your face shows up in lists, rosters, and chat."}
        </p>

        {natural && imgUrl ? (
          <>
            <div
              ref={stageRef}
              className="avatar-editor__stage"
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
            >
              <div className="avatar-editor__canvas" style={bgStyle} />
              <div className="avatar-editor__mask" />
            </div>
            <div className="avatar-editor__controls">
              <span aria-hidden="true">−</span>
              <input
                type="range"
                min={minScale}
                max={maxScale}
                step={0.01}
                value={scale}
                onChange={(e) => setScale(parseFloat(e.target.value))}
                aria-label="Zoom"
              />
              <span aria-hidden="true">+</span>
            </div>
            <p className="avatar-editor__hint">
              Saved as a 512×512 WebP for {userName}.
            </p>
          </>
        ) : (
          <div className="avatar-editor__empty">
            <label className="btn btn--moss">
              Choose image
              <input
                type="file"
                accept="image/*"
                capture="user"
                hidden
                onChange={(e) => onFile(e.target.files?.[0] ?? null)}
              />
            </label>
            {currentUrl && (
              <span className="avatar-editor__hint">
                Or remove the current photo below.
              </span>
            )}
          </div>
        )}

        {error && <p className="muted">{error}</p>}

        <div className="modal__actions">
          {currentUrl && !natural && (
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => remove.mutate()}
              disabled={remove.isPending}
            >
              Remove photo
            </button>
          )}
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => { reset(); onClose(); }}
          >
            Cancel
          </button>
          {natural && (
            <button
              type="button"
              className="btn btn--moss"
              onClick={() => save.mutate()}
              disabled={save.isPending}
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
          )}
        </div>
      </div>
    </dialog>
  );
}
