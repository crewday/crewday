import { Link } from "react-router-dom";

export default function AssetScanPage() {
  return (
    <>
      <section className="phone__section">
        <a href="/today" className="back-link">&larr; Back</a>
      </section>
      <div className="scan-overlay">
        <div className="scan-frame" aria-hidden="true">&#x1F4F7;</div>
        <p className="scan-text">Point camera at asset QR code</p>
        <Link to="/asset/a-villa-ac-bed" className="btn btn--ghost">
          Demo: open Villa Sud AC
        </Link>
      </div>
    </>
  );
}
