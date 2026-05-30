import {
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent as ReactDragEvent,
  type MouseEvent as ReactMouseEvent,
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

  function selectFiles(event: ChangeEvent<HTMLInputElement>): void {
    emitFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  }

  function isFileDrag(types: DOMStringList | readonly string[]): boolean {
    return Array.from(types).includes("Files");
  }

  function openPicker(event: ReactMouseEvent<HTMLButtonElement>): void {
    if (disabled) {
      event.preventDefault();
      return;
    }
    if (event.target !== inputRef.current) inputRef.current?.click();
  }

  function handleDragOver(event: ReactDragEvent<HTMLButtonElement>): void {
    if (!isFileDrag(event.dataTransfer.types)) return;
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

  function handleDrop(event: ReactDragEvent<HTMLButtonElement>): void {
    if (!isFileDrag(event.dataTransfer.types)) return;
    event.preventDefault();
    setDragActive(false);
    if (disabled) return;
    emitFiles(event.dataTransfer.files);
  }

  return (
    <>
      <button
        className={classes}
        type="button"
        aria-disabled={disabled}
        disabled={disabled}
        onClick={openPicker}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <span className="upload-dropzone__title">{title}</span>
        {description ? (
          <span className="upload-dropzone__description">{description}</span>
        ) : null}
      </button>
      <input
        ref={inputRef}
        id={inputId}
        className="upload-dropzone__input"
        type="file"
        accept={accept}
        aria-label={inputLabel ?? title}
        multiple={multiple}
        capture={capture}
        disabled={disabled}
        onChange={selectFiles}
      />
    </>
  );
}
