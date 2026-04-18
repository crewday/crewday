import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  type TextareaHTMLAttributes,
} from "react";

// Drop-in replacement for `<textarea>` that resizes to fit its
// content, capped at `maxHeight` (defaults to 400px). Works with both
// controlled and uncontrolled use; resize fires on every value change.
export interface AutoGrowTextareaProps
  extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  /** Maximum pixel height before the textarea starts scrolling. */
  maxHeight?: number;
}

const AutoGrowTextarea = forwardRef<HTMLTextAreaElement, AutoGrowTextareaProps>(
  function AutoGrowTextarea(
    { maxHeight = 400, rows = 1, onChange, onInput, value, ...rest },
    forwarded,
  ) {
    const localRef = useRef<HTMLTextAreaElement | null>(null);
    useImperativeHandle(forwarded, () => localRef.current as HTMLTextAreaElement);

    const resize = () => {
      const el = localRef.current;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, maxHeight) + "px";
    };

    // Controlled: value prop drives size.
    useLayoutEffect(resize, [value, maxHeight]);

    return (
      <textarea
        ref={localRef}
        rows={rows}
        value={value}
        onChange={(e) => {
          onChange?.(e);
          // Uncontrolled callers don't re-render on input; resize here.
          resize();
        }}
        onInput={(e) => {
          onInput?.(e);
          resize();
        }}
        {...rest}
      />
    );
  },
);

export default AutoGrowTextarea;
