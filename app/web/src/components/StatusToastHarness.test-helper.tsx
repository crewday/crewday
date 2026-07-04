import { StatusToastProvider, useStatusToasts } from "./StatusToast";

export default function StatusToastHarness({ message }: { message: string }) {
  return (
    <StatusToastProvider>
      <Trigger message={message} />
    </StatusToastProvider>
  );
}

function Trigger({ message }: { message: string }) {
  const { enqueueStatusToast } = useStatusToasts();
  return (
    <button type="button" onClick={() => enqueueStatusToast(message)}>
      Enqueue status
    </button>
  );
}
