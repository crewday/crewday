import { type ReactNode, useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";
import type { DisplayError, DisplayErrorDetail } from "@/lib/displayError";

const PROMOTED_PROPERTY_DETAIL_LABELS = new Set([
  "Status",
  "Type",
  "Title",
  "Message",
  "Machine code",
  "Instance",
  "Error ID",
  "Request ID",
]);

interface DisplayErrorDetailsProps {
  error: DisplayError;
  classNamePrefix: string;
}

export default function DisplayErrorDetails({
  error,
  classNamePrefix,
}: DisplayErrorDetailsProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return undefined;
    const timeout = window.setTimeout(() => setCopied(false), 1_800);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  const copyErrorId = () => {
    if (!error.id || !navigator.clipboard) return;
    void navigator.clipboard.writeText(error.id).then(() => setCopied(true), () => undefined);
  };

  const detailRows = error.details.filter((detail) => !isPromotedDetail(detail));

  return (
    <>
      <dl className={`${classNamePrefix}__properties`}>
        <ErrorProperty classNamePrefix={classNamePrefix} label="Error ID" value={error.id}>
          {error.id ? (
            <button
              type="button"
              className={`${classNamePrefix}__copy`}
              aria-label="Copy error ID"
              onClick={copyErrorId}
            >
              {copied ? (
                <>
                  <Check size={13} strokeWidth={2.5} aria-hidden="true" />
                  Copied
                </>
              ) : (
                <>
                  <Copy size={13} strokeWidth={2} aria-hidden="true" />
                  Copy
                </>
              )}
            </button>
          ) : null}
        </ErrorProperty>
        <ErrorProperty
          classNamePrefix={classNamePrefix}
          label="Status"
          value={error.status?.toString() ?? null}
        />
        <ErrorProperty classNamePrefix={classNamePrefix} label="Type" value={error.type} />
        <ErrorProperty classNamePrefix={classNamePrefix} label="Title" value={error.title} />
        <ErrorProperty
          classNamePrefix={classNamePrefix}
          label="Machine code"
          value={error.machineCode}
        />
        <ErrorProperty classNamePrefix={classNamePrefix} label="Instance" value={error.instance} />
        <ErrorProperty
          classNamePrefix={classNamePrefix}
          label="Request ID"
          value={error.requestId}
        />
      </dl>
      <ErrorFieldList classNamePrefix={classNamePrefix} fieldErrors={error.fieldErrors} />
      <ErrorDetailList classNamePrefix={classNamePrefix} details={detailRows} />
    </>
  );
}

function isPromotedDetail(detail: DisplayErrorDetail): boolean {
  if (detail.type === "extension") return false;
  if (detail.label === "Field error") return true;
  return (
    detail.path === null &&
    detail.type === null &&
    PROMOTED_PROPERTY_DETAIL_LABELS.has(detail.label)
  );
}

function ErrorProperty({
  children,
  classNamePrefix,
  label,
  value,
}: {
  children?: ReactNode;
  classNamePrefix: string;
  label: string;
  value: string | null;
}) {
  return (
    <div className={`${classNamePrefix}__property`}>
      <dt>{label}</dt>
      <dd>
        <span>{value ?? "Not provided"}</span>
        {children}
      </dd>
    </div>
  );
}

function ErrorFieldList({
  classNamePrefix,
  fieldErrors,
}: {
  classNamePrefix: string;
  fieldErrors: DisplayError["fieldErrors"];
}) {
  return (
    <section className={`${classNamePrefix}__block`} aria-label="Field errors">
      <h2 className={`${classNamePrefix}__block-title`}>Field errors</h2>
      {fieldErrors.length > 0 ? (
        <ul className={`${classNamePrefix}__list`}>
          {fieldErrors.map((fieldError) => (
            <li key={`${fieldError.loc?.join(".") ?? "field"}-${fieldError.type ?? ""}-${fieldError.msg ?? ""}`}>
              <span>{fieldError.msg ?? "Invalid field"}</span>
              {fieldError.loc?.length ? (
                <code>{fieldError.loc.map(String).join(".")}</code>
              ) : null}
              {fieldError.type ? <small>{fieldError.type}</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className={`${classNamePrefix}__empty`}>None</p>
      )}
    </section>
  );
}

function ErrorDetailList({
  classNamePrefix,
  details,
}: {
  classNamePrefix: string;
  details: ReadonlyArray<DisplayErrorDetail>;
}) {
  return (
    <section className={`${classNamePrefix}__block`} aria-label="Details">
      <h2 className={`${classNamePrefix}__block-title`}>Details</h2>
      {details.length > 0 ? (
        <ul className={`${classNamePrefix}__list`}>
          {details.map((detail, index) => (
            <li key={`${detail.label}-${detail.path ?? "detail"}-${index}`}>
              <span>{detail.label}: {detail.message}</span>
              {detail.path ? <code>{detail.path}</code> : null}
              {detail.type ? <small>{detail.type}</small> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className={`${classNamePrefix}__empty`}>None</p>
      )}
    </section>
  );
}
