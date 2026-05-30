import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

const BUTTON_ROLE = "button";

interface FileDropZoneProps {
  title: string;
  description?: string;
  inputLabel?: string;
  accept?: string;
  multiple?: boolean;
  capture?: boolean | "user" | "environment";
  disabled?: boolean;
  className?: string;
  onFiles: (files: File[]) => void;
}

export default function FileDropZone(props: FileDropZoneProps) {
  const {
    title,
    description,
    inputLabel,
    accept,
    multiple = false,
    capture,
    disabled = false,
    className,
    onFiles,
  } = props;
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const labelRef = useRef<HTMLLabelElement>(null);
  const [dragActive, setDragActive] = useState(false);
  const classes = [
    "upload-dropzone",
    dragActive ? "upload-dropzone--active" : "",
    disabled ? "upload-dropzone--disabled" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  const emitFiles = useCallback((files: FileList | File[] | null): void => {
    if (disabled || !files?.length) return;
    onFiles(Array.from(files));
  }, [disabled, onFiles]);

  function handleChange(event: ChangeEvent<HTMLInputElement>): void {
    emitFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  }

  function isFileDrag(event: globalThis.DragEvent): boolean {
    return event.dataTransfer?.types.includes("Files") ?? false;
  }

  useEffect(() => {
    const label = labelRef.current;
    if (!label) return undefined;

    function handleClick(event: MouseEvent): void {
      if (disabled) event.preventDefault();
    }

    function handleDragOver(event: globalThis.DragEvent): void {
      const dataTransfer = event.dataTransfer;
      if (!dataTransfer || !isFileDrag(event)) return;
      event.preventDefault();
      if (disabled) {
        dataTransfer.dropEffect = "none";
        setDragActive(false);
        return;
      }
      setDragActive(true);
      dataTransfer.dropEffect = "copy";
    }

    function handleDragLeave(): void {
      setDragActive(false);
    }

    function handleDrop(event: globalThis.DragEvent): void {
      const dataTransfer = event.dataTransfer;
      if (!dataTransfer || !isFileDrag(event)) return;
      event.preventDefault();
      setDragActive(false);
      if (disabled) return;
      emitFiles(dataTransfer.files);
    }

    function handleKeyDown(event: KeyboardEvent): void {
      if (disabled) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      inputRef.current?.click();
    }

    label.addEventListener("click", handleClick);
    label.addEventListener("dragover", handleDragOver);
    label.addEventListener("dragleave", handleDragLeave);
    label.addEventListener("drop", handleDrop);
    label.addEventListener("keydown", handleKeyDown);
    return () => {
      label.removeEventListener("click", handleClick);
      label.removeEventListener("dragover", handleDragOver);
      label.removeEventListener("dragleave", handleDragLeave);
      label.removeEventListener("drop", handleDrop);
      label.removeEventListener("keydown", handleKeyDown);
    };
  }, [disabled, emitFiles]);

  return (
    <label
      ref={labelRef}
      className={classes}
      htmlFor={inputId}
      role={BUTTON_ROLE}
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : 0}
    >
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        accept={accept}
        aria-label={inputLabel}
        multiple={multiple}
        capture={capture}
        disabled={disabled}
        onChange={handleChange}
      />
      <span className="upload-dropzone__title">{title}</span>
      {description ? (
        <span className="upload-dropzone__description">{description}</span>
      ) : null}
    </label>
  );
}
