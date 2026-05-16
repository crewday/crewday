import { AlertTriangle } from "lucide-react";
import { useId, type ReactNode } from "react";
import FormModal from "@/components/FormModal";

type ConfirmationTone = "moss" | "rust";

interface ConfirmationModalProps {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  tone?: ConfirmationTone;
  eyebrow?: string;
  pending?: boolean;
}

export default function ConfirmationModal({
  open,
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  tone = "rust",
  eyebrow = "Confirm change",
  pending = false,
}: ConfirmationModalProps) {
  const titleId = useId();
  const descriptionId = useId();

  return (
    <FormModal
      open={open}
      title={title}
      titleId={titleId}
      describedBy={descriptionId}
      eyebrow={eyebrow}
      width="narrow"
      contentElement="section"
      bodyClassName="confirmation-modal__body"
      footerClassName="confirmation-modal__footer"
      closeDisabled={pending}
      dialogRole="alertdialog"
      onClose={onCancel}
      onCancel={(event) => {
        if (pending) {
          event.preventDefault();
          return;
        }
        onCancel();
      }}
      actions={
        <>
          <button
            type="button"
            className="btn btn--ghost"
            disabled={pending}
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className={`btn btn--${tone}`}
            disabled={pending}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <div className={`confirmation-modal__mark confirmation-modal__mark--${tone}`}>
        <AlertTriangle aria-hidden="true" size={20} />
      </div>
      <div id={descriptionId} className="confirmation-modal__copy">
        {children}
      </div>
    </FormModal>
  );
}
