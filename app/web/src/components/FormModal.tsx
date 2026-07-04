import {
  useId,
  useLayoutEffect,
  useRef,
  type FormEventHandler,
  type HTMLAttributes,
  type ReactEventHandler,
  type ReactNode,
} from "react";
import FormField, { type FieldRequirement } from "@/components/FormField";
import { closeModalDialog, openModalDialog } from "@/lib/modalDialog";

type FormModalWidth = "default" | "narrow" | "wide";
type FormModalRole = "dialog" | "alertdialog";

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
  dialogRole?: FormModalRole;
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
    dialogRole,
    onSubmit,
    onCancel,
  } = props;
  const generatedTitleId = useId();
  const resolvedTitleId = titleId ?? generatedTitleId;
  const dialogRef = useRef<HTMLDialogElement>(null);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) openModalDialog(dialog);
    else closeModalDialog(dialog);
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
      role={dialogRole}
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
