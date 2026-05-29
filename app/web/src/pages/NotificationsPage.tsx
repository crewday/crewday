import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCheck, Mail, MailOpen } from "lucide-react";
import ChatMessageBody from "@/components/chat/ChatMessageBody";
import DateTime from "@/components/DateTime";
import PageHeader from "@/components/PageHeader";
import { Chip, EmptyState, Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { NotificationListResponse, NotificationPayload } from "@/types/api";

const NOTIFICATIONS_URL = "/api/v1/messaging/notifications";

function payloadText(notification: NotificationPayload, key: string): string | null {
  const value = notification.payload[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function notificationTitle(notification: NotificationPayload): string {
  return (
    notification.subject.trim() ||
    payloadText(notification, "broadcast_subject") ||
    payloadText(notification, "preview") ||
    "Notification"
  );
}

function notificationBody(notification: NotificationPayload): string | null {
  if (notification.body_md?.trim()) return notification.body_md.trim();
  return payloadText(notification, "message_body") ?? payloadText(notification, "preview");
}

function notificationKindLabel(kind: string): string {
  return kind.replace(/_/g, " ");
}

function mergeNotifications(
  current: NotificationListResponse | undefined,
  updates: NotificationPayload[],
): NotificationListResponse | undefined {
  if (!current) return current;
  const byId = new Map(updates.map((notification) => [notification.id, notification]));
  return {
    ...current,
    data: current.data.map((notification) => byId.get(notification.id) ?? notification),
  };
}

export default function NotificationsPage() {
  const qc = useQueryClient();
  const queryKey = qk.notifications();
  const q = useQuery({
    queryKey,
    queryFn: () => fetchJson<NotificationListResponse>(`${NOTIFICATIONS_URL}?limit=100`),
  });
  const notifications = q.data?.data ?? [];
  const visibleUnreadIds = notifications
    .filter((notification) => notification.read_at === null)
    .map((notification) => notification.id);

  const patchRead = useMutation<
    NotificationPayload,
    Error,
    { id: string; read: boolean },
    { previous?: NotificationListResponse }
  >({
    mutationFn: ({ id, read }: { id: string; read: boolean }) =>
      fetchJson<NotificationPayload>(`${NOTIFICATIONS_URL}/${id}`, {
        method: "PATCH",
        body: { read },
      }),
    onMutate: async ({ id, read }) => {
      await qc.cancelQueries({ queryKey });
      const previous = qc.getQueryData<NotificationListResponse>(queryKey);
      const readAt = read ? new Date().toISOString() : null;
      qc.setQueryData<NotificationListResponse>(queryKey, (current) => {
        if (!current) return current;
        return {
          ...current,
          data: current.data.map((notification) =>
            notification.id === id
              ? { ...notification, read_at: readAt }
              : notification
          ),
        };
      });
      return { previous };
    },
    onError: (_error, _variables, context) => {
      if (context?.previous) qc.setQueryData(queryKey, context.previous);
    },
    onSuccess: (updated) => {
      qc.setQueryData<NotificationListResponse>(queryKey, (current) =>
        mergeNotifications(current, [updated])
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey }),
  });

  const markVisibleRead = useMutation<
    NotificationListResponse,
    Error,
    string[],
    { previous?: NotificationListResponse }
  >({
    mutationFn: (ids: string[]) =>
      fetchJson<NotificationListResponse>(`${NOTIFICATIONS_URL}:mark-read`, {
        method: "POST",
        body: { ids },
      }),
    onMutate: async (ids) => {
      await qc.cancelQueries({ queryKey });
      const previous = qc.getQueryData<NotificationListResponse>(queryKey);
      const readAt = new Date().toISOString();
      const visibleIds = new Set(ids);
      qc.setQueryData<NotificationListResponse>(queryKey, (current) => {
        if (!current) return current;
        return {
          ...current,
          data: current.data.map((notification) =>
            visibleIds.has(notification.id)
              ? { ...notification, read_at: readAt }
              : notification
          ),
        };
      });
      return { previous };
    },
    onError: (_error, _ids, context) => {
      if (context?.previous) qc.setQueryData(queryKey, context.previous);
    },
    onSuccess: (updated) => {
      qc.setQueryData<NotificationListResponse>(queryKey, (current) =>
        mergeNotifications(current, updated.data)
      );
    },
    onSettled: () => qc.invalidateQueries({ queryKey }),
  });

  const actions = (
    <button
      type="button"
      className="btn btn--moss"
      disabled={visibleUnreadIds.length === 0 || markVisibleRead.isPending}
      onClick={() => markVisibleRead.mutate(visibleUnreadIds)}
    >
      <CheckCheck size={16} strokeWidth={1.8} aria-hidden="true" />
      Mark visible read
    </button>
  );

  return (
    <>
      <PageHeader
        title="Notifications"
        sub="Messages and workspace alerts sent to you."
        actions={actions}
        back={false}
      />
      <section className="notifications-page">
        {q.isPending ? <Loading /> : null}
        {q.isError ? <p className="form-error">Could not load notifications.</p> : null}
        {!q.isPending && !q.isError && notifications.length === 0 ? (
          <EmptyState title="No notifications" copy="New messages and alerts will appear here." />
        ) : null}
        {notifications.length > 0 ? (
          <ul className="notification-list" aria-label="Notifications">
            {notifications.map((notification) => (
              <NotificationItem
                key={notification.id}
                notification={notification}
                pending={patchRead.isPending}
                onToggleRead={(read) => patchRead.mutate({ id: notification.id, read })}
              />
            ))}
          </ul>
        ) : null}
      </section>
    </>
  );
}

function NotificationItem({
  notification,
  pending,
  onToggleRead,
}: {
  notification: NotificationPayload;
  pending: boolean;
  onToggleRead: (read: boolean) => void;
}) {
  const isUnread = notification.read_at === null;
  const body = notificationBody(notification);
  return (
    <li
      className={
        "notification-card" +
        (isUnread ? " notification-card--unread" : " notification-card--read")
      }
    >
      <div className="notification-card__status" aria-hidden="true">
        {isUnread ? <Mail size={18} strokeWidth={1.8} /> : <MailOpen size={18} strokeWidth={1.8} />}
      </div>
      <article className="notification-card__body" aria-label={notificationTitle(notification)}>
        <div className="notification-card__head">
          <div className="notification-card__meta">
            <Chip tone={isUnread ? "moss" : "ghost"} size="sm">
              {isUnread ? "Unread" : "Read"}
            </Chip>
            <span>{notificationKindLabel(notification.kind)}</span>
            <DateTime value={notification.created_at} showTime className="mono" />
          </div>
          <button
            type="button"
            className="btn btn--ghost btn--sm notification-card__toggle"
            disabled={pending}
            onClick={() => onToggleRead(isUnread)}
          >
            {isUnread ? (
              <MailOpen size={14} strokeWidth={1.8} aria-hidden="true" />
            ) : (
              <Mail size={14} strokeWidth={1.8} aria-hidden="true" />
            )}
            {isUnread ? "Mark read" : "Mark unread"}
          </button>
        </div>
        <h2 className="notification-card__title">{notificationTitle(notification)}</h2>
        {body ? (
          <ChatMessageBody body={body} className="notification-card__message" />
        ) : null}
      </article>
    </li>
  );
}
