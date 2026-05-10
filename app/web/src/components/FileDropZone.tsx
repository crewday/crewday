import {
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";

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
  const [dragActive, setDragActive] = useState(false);
  const classes = [
    "upload-dropzone",
    dragActive ? "upload-dropzone--active" : "",
    disabled ? "upload-dropzone--disabled" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  function emitFiles(files: FileList | File[] | null): void {
    if (disabled || !files?.length) return;
    onFiles(Array.from(files));
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>): void {
    emitFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  }

  function isFileDrag(event: DragEvent<HTMLLabelElement>): boolean {
    return event.dataTransfer.types.includes("Files");
  }

  function handleClick(event: MouseEvent<HTMLLabelElement>): void {
    if (disabled) event.preventDefault();
  }

  function handleDragOver(event: DragEvent<HTMLLabelElement>): void {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    if (disabled) {
      event.dataTransfer.dropEffect = "none";
      setDragActive(false);
      return;
    }
    setDragActive(true);
    event.dataTransfer.dropEffect = "copy";
  }

  function handleDragLeave(): void {
    setDragActive(false);
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>): void {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    setDragActive(false);
    if (disabled) return;
    emitFiles(event.dataTransfer.files);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLLabelElement>): void {
    if (disabled) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    inputRef.current?.click();
  }

  return (
    <label
      className={classes}
      htmlFor={inputId}
      role="button"
      aria-disabled={disabled}
      tabIndex={disabled ? -1 : 0}
      onClick={handleClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onKeyDown={handleKeyDown}
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
