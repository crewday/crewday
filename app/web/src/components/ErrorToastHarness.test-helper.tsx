import type { DisplayError } from "@/lib/displayError";
import { ErrorToastProvider, useErrorToasts } from "./ErrorToast";

export default function ErrorToastHarness({ error }: { error: DisplayError }) {
  return (
    <ErrorToastProvider>
      <Trigger error={error} />
    </ErrorToastProvider>
  );
}

function Trigger({ error }: { error: DisplayError }) {
  const { enqueueErrorToast } = useErrorToasts();
  return (
    <button type="button" onClick={() => enqueueErrorToast(error)}>
      Enqueue error
    </button>
  );
}
