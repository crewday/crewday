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
  children: ReactNode;
  onClose: () => void;
  actions?: ReactNode;
  eyebrow?: ReactNode;
  subtitle?: ReactNode;
  titleId?: string;
  describedBy?: string;
  width?: FormModalWidth;
  className?: string;
  formClassName?: string;
  bodyClassName?: string;
  footerClassName?: string;
  closeLabel?: string;
  closeDisabled?: boolean;
  contentElement?: "form" | "section";
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
    describedBy,
    width = "default",
    className,
    formClassName,
    bodyClassName,
    footerClassName,
    closeLabel = "Close",
    closeDisabled = false,
    contentElement = "form",
    noValidate = false,
    onSubmit,
    onCancel,
  } = props;
  const generatedTitleId = useId();
  const resolvedTitleId = titleId ?? generatedTitleId;
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (open && dialog && !dialog.open) {
      if (typeof dialog.showModal === "function") {
        try {
          dialog.showModal();
        } catch {
          dialog.setAttribute("open", "");
        }
      } else {
        dialog.setAttribute("open", "");
      }
    }
    if (!open && dialog?.open) {
      if (typeof dialog.close === "function") {
        try {
          dialog.close();
        } catch {
          dialog.removeAttribute("open");
        }
      } else {
        dialog.removeAttribute("open");
      }
    }
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
      aria-describedby={describedBy}
      onCancel={onCancel}
      onClose={onClose}
    >
      {open && contentElement === "section" ? (
        <section className={classes("form-modal", formClassName)}>
          <FormModalContent
            title={title}
            actions={actions}
            onClose={onClose}
            eyebrow={eyebrow}
            subtitle={subtitle}
            titleId={resolvedTitleId}
            bodyClassName={bodyClassName}
            footerClassName={footerClassName}
            closeLabel={closeLabel}
            closeDisabled={closeDisabled}
          >
            {children}
          </FormModalContent>
        </section>
      ) : null}
      {open && contentElement === "form" ? (
        <form
          className={classes("form-modal", formClassName)}
          onSubmit={onSubmit}
          noValidate={noValidate}
        >
          <FormModalContent
            title={title}
            actions={actions}
            onClose={onClose}
            eyebrow={eyebrow}
            subtitle={subtitle}
            titleId={resolvedTitleId}
            bodyClassName={bodyClassName}
            footerClassName={footerClassName}
            closeLabel={closeLabel}
            closeDisabled={closeDisabled}
          >
            {children}
          </FormModalContent>
        </form>
      ) : null}
    </dialog>
  );
}

function FormModalContent({
  title,
  actions,
  children,
  onClose,
  eyebrow,
  subtitle,
  titleId,
  bodyClassName,
  footerClassName,
  closeLabel,
  closeDisabled,
}: Pick<
  FormModalProps,
  | "title"
  | "actions"
  | "children"
  | "onClose"
  | "eyebrow"
  | "subtitle"
  | "bodyClassName"
  | "footerClassName"
  | "closeLabel"
  | "closeDisabled"
> & { titleId: string }) {
  return (
    <>
      <header className="form-modal__head">
        <div>
          {eyebrow ? <p className="form-modal__eyebrow">{eyebrow}</p> : null}
          <h3 id={titleId} className="form-modal__title">
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

      <div className={classes("form-modal__body", bodyClassName)}>{children}</div>

      {actions != null ? (
        <footer className={classes("form-modal__footer", footerClassName)}>
          {actions}
        </footer>
      ) : null}
    </>
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
