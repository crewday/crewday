import {
  useEffect,
  useId,
  useRef,
  type FormEventHandler,
  type HTMLAttributes,
  type ReactEventHandler,
  type ReactNode,
} from "react";
import FormField, { type FieldRequirement } from "@/components/FormField";

type FormModalWidth = "default" | "narrow" | "wide";

interface FormModalProps {
  open: boolean;
  title: ReactNode;
  actions: ReactNode;
  children: ReactNode;
  onClose: () => void;
  eyebrow?: ReactNode;
  subtitle?: ReactNode;
  titleId?: string;
  width?: FormModalWidth;
  className?: string;
  formClassName?: string;
  bodyClassName?: string;
  footerClassName?: string;
  closeLabel?: string;
  closeDisabled?: boolean;
  noValidate?: boolean;
  onSubmit?: FormEventHandler<HTMLFormElement>;
  onCancel?: ReactEventHandler<HTMLDialogElement>;
}

interface FormModalFieldProps {
  label: string;
  requirement: FieldRequirement;
  children: ReactNode;
  className?: string;
  helpId?: string;
  helpText?: ReactNode;
}

function classes(...values: (string | false | null | undefined)[]): string {
  return values.filter(Boolean).join(" ");
}

export default function FormModal(props: FormModalProps) {
  const {
    open,
    title,
    actions,
    children,
    onClose,
    eyebrow,
    subtitle,
    titleId,
    width = "default",
    className,
    formClassName,
    bodyClassName,
    footerClassName,
    closeLabel = "Close",
    closeDisabled = false,
    noValidate = false,
    onSubmit,
    onCancel,
  } = props;
  const generatedTitleId = useId();
  const resolvedTitleId = titleId ?? generatedTitleId;
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) dialog.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      className={classes(
        "modal modal--sheet form-modal-dialog",
        width !== "default" && `form-modal-dialog--${width}`,
        className,
      )}
      aria-labelledby={resolvedTitleId}
      onCancel={onCancel}
      onClose={onClose}
    >
      {open ? (
        <form
          className={classes("form-modal", formClassName)}
          onSubmit={onSubmit}
          noValidate={noValidate}
        >
          <header className="form-modal__head">
            <div>
              {eyebrow ? <p className="form-modal__eyebrow">{eyebrow}</p> : null}
              <h3 id={resolvedTitleId} className="form-modal__title">
                {title}
              </h3>
              {subtitle ? <p className="form-modal__sub">{subtitle}</p> : null}
            </div>
            <button
              type="button"
              className="form-modal__close"
              disabled={closeDisabled}
              onClick={onClose}
              aria-label={closeLabel}
            >
              ×
            </button>
          </header>

          <div className={classes("form-modal__body", bodyClassName)}>
            {children}
          </div>

          <footer className={classes("form-modal__footer", footerClassName)}>
            {actions}
          </footer>
        </form>
      ) : null}
    </dialog>
  );
}

export function FormModalField(props: FormModalFieldProps) {
  const { className, ...fieldProps } = props;
  return (
    <FormField
      {...fieldProps}
      className={classes("form-modal__field", className)}
    />
  );
}

export function FormModalGrid({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={classes("form-modal__grid", className)} />;
}
