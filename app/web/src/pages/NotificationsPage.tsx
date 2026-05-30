import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { Mail, MailOpen } from "lucide-react";
import ChatMessageBody from "@/components/chat/ChatMessageBody";
import DateTime from "@/components/DateTime";
import PageHeader from "@/components/PageHeader";
import { EmptyState, Loading } from "@/components/common";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import type { NotificationListResponse, NotificationPayload } from "@/types/api";

const NOTIFICATIONS_URL = "/api/v1/messaging/notifications";
const EMPTY_NOTIFICATIONS: NotificationPayload[] = [];
const autoMarkedNotificationSignatures = new Set<string>();

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
  const visuallyUnreadIdsRef = useRef<Set<string>>(new Set());
  const q = useQuery({
    queryKey,
    queryFn: () => fetchJson<NotificationListResponse>(`${NOTIFICATIONS_URL}?limit=100`),
  });
  const notifications = q.data?.data ?? EMPTY_NOTIFICATIONS;
  const visibleUnreadIds = useMemo(
    () =>
      notifications
        .filter((notification) => notification.read_at === null)
        .map((notification) => notification.id),
    [notifications],
  );
  const visibleUnreadSignature = visibleUnreadIds.join("\u001f");

  const markVisibleRead = useMutation<NotificationListResponse, Error, string[]>({
    mutationFn: (ids: string[]) =>
      fetchJson<NotificationListResponse>(`${NOTIFICATIONS_URL}:mark-read`, {
        method: "POST",
        body: { ids },
      }),
    onSuccess: (updated) => {
      qc.setQueryData<NotificationListResponse>(queryKey, (current) =>
        mergeNotifications(current, updated.data),
      );
    },
  });

  useEffect(() => {
    if (!q.isSuccess || visibleUnreadIds.length === 0) return;
    const autoMarkSignature = JSON.stringify([...queryKey, visibleUnreadSignature]);
    if (autoMarkedNotificationSignatures.has(autoMarkSignature)) return;
    autoMarkedNotificationSignatures.add(autoMarkSignature);
    visuallyUnreadIdsRef.current = new Set([
      ...visuallyUnreadIdsRef.current,
      ...visibleUnreadIds,
    ]);
    markVisibleRead.mutate(visibleUnreadIds);
  }, [markVisibleRead, q.isSuccess, queryKey, visibleUnreadIds, visibleUnreadSignature]);

  return (
    <>
      <PageHeader
        title="Notifications"
        sub="Messages and workspace alerts sent to you."
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
                forceUnread={visuallyUnreadIdsRef.current.has(notification.id)}
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
  forceUnread,
}: {
  notification: NotificationPayload;
  forceUnread: boolean;
}) {
  const isUnread = forceUnread || notification.read_at === null;
  const body = notificationBody(notification);
  return (
    <li
      className={
        "notification-card" +
        (isUnread ? " notification-card--unread" : " notification-card--read")
      }
    >
      <div
        className="notification-card__status"
        aria-label={isUnread ? "Unread notification" : "Read notification"}
        role="img"
      >
        {isUnread ? <Mail size={18} strokeWidth={1.8} /> : <MailOpen size={18} strokeWidth={1.8} />}
      </div>
      <article className="notification-card__body" aria-label={notificationTitle(notification)}>
        <div className="notification-card__head">
          <div className="notification-card__meta">
            <span>{notificationKindLabel(notification.kind)}</span>
            <DateTime value={notification.created_at} showTime className="mono" />
          </div>
        </div>
        <h2 className="notification-card__title">{notificationTitle(notification)}</h2>
        {body ? (
          <ChatMessageBody body={body} className="notification-card__message" />
        ) : null}
      </article>
    </li>
  );
}
