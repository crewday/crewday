import { BrowserQRCodeReader, type IScannerControls } from "@zxing/browser";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import PageHeader from "@/components/PageHeader";

interface AssetScanResponse {
  id: string;
}

type ScanStatus = "starting" | "scanning" | "manual" | "resolving";

const QR_TOKEN_RE = /^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{12}$/;

function tokenFromQr(raw: string): string | null {
  let candidate = raw.trim();
  if (!candidate) return null;
  try {
    const url = new URL(candidate, window.location.origin);
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts.length >= 3 && parts[parts.length - 3] === "asset" && parts[parts.length - 2] === "scan") {
      candidate = decodeURIComponent(parts[parts.length - 1] ?? "");
    }
  } catch {
    // Fall through to plain-token handling.
  }
  const token = candidate.trim().toUpperCase();
  return QR_TOKEN_RE.test(token) ? token : null;
}

function scanText(status: ScanStatus): string {
  if (status === "starting") return "Allow camera access to scan asset QR codes";
  if (status === "resolving") return "Opening asset";
  if (status === "manual") return "Enter the QR code printed on the asset label";
  return "Point camera at asset QR code";
}

export default function AssetScanPage() {
  const navigate = useNavigate();
  const { token: routeToken } = useParams<{ token?: string }>();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const resolvingRef = useRef(false);
  const [status, setStatus] = useState<ScanStatus>("starting");
  const [manualCode, setManualCode] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const openScan = useCallback(async (raw: string): Promise<void> => {
    const token = tokenFromQr(raw);
    if (!token) {
      resolvingRef.current = false;
      setStatus("manual");
      setNotice("Enter the 12-character QR code printed on the asset label.");
      return;
    }
    resolvingRef.current = true;
    setStatus("resolving");
    setNotice(null);
    try {
      const asset = await fetchJson<AssetScanResponse>(
        "/api/v1/asset/scan/" + encodeURIComponent(token),
      );
      navigate("/asset/" + asset.id);
    } catch {
      resolvingRef.current = false;
      setStatus("manual");
      setNotice("This asset is not registered here.");
    }
  }, [navigate]);

  useEffect(() => {
    if (routeToken) {
      void openScan(routeToken);
      return undefined;
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus("manual");
      setNotice("Camera scanning is not available on this device.");
      return undefined;
    }

    let cancelled = false;
    let controls: IScannerControls | null = null;
    let stoppedForResult = false;

    const start = async (): Promise<void> => {
      const video = videoRef.current;
      if (!video) {
        setStatus("manual");
        setNotice("Camera scanning is not available on this device.");
        return;
      }
      const reader = new BrowserQRCodeReader();
      try {
        controls = await reader.decodeFromConstraints(
          { video: { facingMode: { ideal: "environment" } }, audio: false },
          video,
          (result, _error, scanControls) => {
            if (cancelled || resolvingRef.current || !result) return;
            stoppedForResult = true;
            scanControls.stop();
            controls = null;
            void openScan(result.getText());
          },
        );
        if (stoppedForResult) {
          controls = null;
          return;
        }
        if (cancelled) {
          controls.stop();
          controls = null;
          return;
        }
        setStatus("scanning");
        setNotice(null);
      } catch {
        if (!cancelled) {
          setStatus("manual");
          setNotice("Camera access was blocked. Enter the QR code instead.");
        }
      }
    };

    void start();

    return () => {
      cancelled = true;
      controls?.stop();
      BrowserQRCodeReader.releaseAllStreams();
    };
  }, [openScan, routeToken]);

  const submitManual = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void openScan(manualCode);
  };

  const showManual = status === "manual" || notice !== null;

  return (
    <>
      <PageHeader title="Scan asset" />
      <div className="scan-overlay">
        <div className="scan-frame">
          <video
            ref={videoRef}
            className={
              "scan-video"
              + (status === "scanning" || status === "resolving" ? " scan-video--active" : "")
            }
            muted
            playsInline
            aria-label="Asset QR camera preview"
          />
          {status === "scanning" || status === "resolving" ? null : (
            <span aria-hidden="true">&#x1F4F7;</span>
          )}
        </div>
        <p className="scan-text">{scanText(status)}</p>
        {notice ? <p className="scan-notice" role="alert">{notice}</p> : null}
        {showManual ? (
          <form className="scan-manual" onSubmit={submitManual}>
            <label className="scan-manual__field">
              <span>QR code</span>
              <input
                value={manualCode}
                onChange={(event) => setManualCode(event.currentTarget.value)}
                autoCapitalize="characters"
                autoComplete="off"
                inputMode="text"
              />
            </label>
            <button className="btn btn--moss" type="submit" disabled={status === "resolving"}>
              Open asset
            </button>
          </form>
        ) : null}
        {import.meta.env.DEV ? (
          <Link to="/asset/a-villa-ac-bed" className="btn btn--ghost">
            Demo: open Villa Sud AC
          </Link>
        ) : null}
      </div>
    </>
  );
}
