/**
 * First-article approval banner (farm production, Phase 3; self-contained in
 * Phase 4, F1).
 *
 * Extracted verbatim from ProductionRunsPage so BOTH the runs list and the run
 * detail page render the same approve/reject controls, then extended so the
 * approval is self-contained: the finished part's photo and a collapsed-by-
 * default live camera view (phone-friendly) sit inline with the actions, so a
 * remote approver no longer has to judge blind.
 *
 * Driven by `run.first_article_state`:
 *  - pending_print:     subtle "printing" badge
 *  - awaiting_approval: prominent banner with the part photo + camera toggle and
 *                       Approve (physical) / Approve & eject / Reject… — every
 *                       mutation confirmed, and every failure surfaced inline
 *                       (role="alert").
 *  - rejected:          red badge + reason + the rejected part's photo/camera +
 *                       "Resume restarts a new first article" hint
 *  - approved / null:   a small check (approved) or nothing
 */
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, AlertTriangle, CheckCircle2, Camera, CameraOff, Loader2, X, XCircle } from 'lucide-react';
import { api, withStreamToken } from '../api/client';
import { Card, CardContent } from './Card';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { CameraTile } from './CameraTile';
import { useToast } from '../contexts/ToastContext';
import type { ProductionRun } from '../types/productionRuns';

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary ' +
  'focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

/**
 * First-article inspection aids (Phase 4, F1): the finished part's finish photo
 * (tap → full-size in a new tab; degrades to a "photo unavailable" note if the
 * image 403s for a user without archive perms or was pruned) and a
 * collapsed-by-default live camera view of the printer that produced it. The
 * camera stays off until the operator opts in — remote approvals often happen on
 * a phone, so we never pull a stream unprompted. Renders nothing when the run
 * carries neither a photo nor a printer.
 */
function FirstArticleInspection({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  const [imgError, setImgError] = useState(false);
  const [showCamera, setShowCamera] = useState(false);

  const printerId = run.first_article_printer_id ?? null;
  const printerName = run.first_article_printer_name ?? null;
  const photoUrl = run.first_article_photo_url ? withStreamToken(run.first_article_photo_url) : null;

  const cameraStatus = useQuery({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId as number),
    enabled: showCamera && printerId != null,
    refetchInterval: 30000,
  });

  if (!photoUrl && printerId == null) return null;

  return (
    <div className="mt-3 space-y-2">
      {photoUrl &&
        (imgError ? (
          <p className="text-xs text-bambu-gray">{t('productionRuns.firstArticle.photoUnavailable')}</p>
        ) : (
          <a
            href={photoUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block rounded-lg focus:outline-none focus:ring-2 focus:ring-bambu-green"
          >
            <img
              src={photoUrl}
              alt={t('productionRuns.firstArticle.photoAlt')}
              className="max-h-48 w-auto rounded-lg border border-bambu-dark-tertiary object-contain"
              onError={() => setImgError(true)}
            />
          </a>
        ))}

      {printerId != null && (
        <div>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setShowCamera((s) => !s)}
            aria-expanded={showCamera}
          >
            {showCamera ? (
              <CameraOff className="h-4 w-4" />
            ) : (
              <Camera className="h-4 w-4" />
            )}
            {showCamera
              ? t('productionRuns.firstArticle.hideCamera')
              : t('productionRuns.firstArticle.viewCamera')}
          </Button>
          {showCamera && (
            <div className="mt-2 max-w-sm space-y-1">
              <CameraTile
                printerId={printerId}
                printerName={printerName ?? ''}
                mode="snapshot"
                snapshotIntervalMs={5000}
                connected={cameraStatus.data?.connected ?? false}
              />
              {printerName && (
                <p className="text-xs text-bambu-gray">
                  {t('productionRuns.firstArticle.printerLabel', { name: printerName })}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function FirstArticleBanner({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const state = run.first_article_state;

  const [confirm, setConfirm] = useState<'physical' | 'eject' | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState(false);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['production-runs'] });

  const approveMutation = useMutation({
    mutationFn: (ejectRemotely: boolean) =>
      api.approveFirstArticle(run.id, { eject_remotely: ejectRemotely }),
    onSuccess: () => {
      showToast(t('productionRuns.firstArticle.approved'));
      invalidate();
      setConfirm(null);
    },
    // On failure close the confirm dialog so the inline banner alert (below)
    // is visible; the 5s poll reconciles state either way.
    onError: () => setConfirm(null),
  });

  const rejectMutation = useMutation({
    mutationFn: (r: string) => api.rejectFirstArticle(run.id, { reason: r }),
    onSuccess: () => {
      showToast(t('productionRuns.firstArticle.rejected'));
      invalidate();
      setRejectOpen(false);
      setReason('');
    },
    // Error stays in the still-open reject dialog (role="alert" there).
  });

  const openReject = () => {
    rejectMutation.reset();
    setReason('');
    setReasonError(false);
    setRejectOpen(true);
  };

  const submitReject = (e: FormEvent) => {
    e.preventDefault();
    if (reason.trim() === '') {
      setReasonError(true);
      return;
    }
    rejectMutation.mutate(reason.trim());
  };

  if (state == null) return null;

  if (state === 'pending_print') {
    return (
      <span className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-bambu-dark-tertiary bg-bambu-dark px-2.5 py-1 text-xs font-medium text-bambu-gray">
        <Loader2 className="w-3.5 h-3.5 animate-spin" />
        {t('productionRuns.firstArticle.printing')}
      </span>
    );
  }

  if (state === 'approved') {
    return (
      <span className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-bambu-green/30 bg-bambu-green/15 px-2.5 py-1 text-xs font-medium text-bambu-green">
        <CheckCircle2 className="w-3.5 h-3.5" />
        {t('productionRuns.firstArticle.approvedBadge')}
      </span>
    );
  }

  if (state === 'rejected') {
    return (
      <div className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3">
        <span className="inline-flex items-center gap-1.5 text-sm font-medium text-red-300">
          <XCircle className="w-4 h-4" />
          {t('productionRuns.firstArticle.rejectedBadge')}
        </span>
        {run.first_article_reject_reason ? (
          <p className="mt-1 text-sm text-red-200/90">
            {t('productionRuns.firstArticle.rejectedReason', {
              reason: run.first_article_reject_reason,
            })}
          </p>
        ) : null}
        <p className="mt-1 text-xs text-bambu-gray">
          {t('productionRuns.firstArticle.rejectedHint')}
        </p>
        <FirstArticleInspection run={run} />
      </div>
    );
  }

  // awaiting_approval
  return (
    <>
      <div className="mt-3 rounded-lg border border-yellow-500/40 bg-yellow-500/10 p-3">
        <div className="flex items-start gap-2">
          <AlertTriangle className="w-5 h-5 flex-shrink-0 text-yellow-300" />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-yellow-200">
              {t('productionRuns.firstArticle.awaitingTitle')}
            </p>
            <p className="mt-0.5 text-xs text-yellow-200/80">
              {t('productionRuns.firstArticle.awaitingBody')}
            </p>
          </div>
        </div>

        <FirstArticleInspection run={run} />

        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            size="sm"
            onClick={() => {
              approveMutation.reset();
              setConfirm('physical');
            }}
            disabled={approveMutation.isPending}
          >
            {t('productionRuns.firstArticle.approvePhysical')}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              approveMutation.reset();
              setConfirm('eject');
            }}
            disabled={approveMutation.isPending}
          >
            {t('productionRuns.firstArticle.approveEject')}
          </Button>
          <Button
            size="sm"
            variant="danger"
            onClick={openReject}
            disabled={approveMutation.isPending}
          >
            {t('productionRuns.firstArticle.reject')}
          </Button>
        </div>

        {approveMutation.error && (
          <div
            role="alert"
            className="mt-3 flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/10 p-2.5 text-sm text-red-300"
          >
            <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
            <span>
              {approveMutation.error.message || t('productionRuns.firstArticle.approveFailed')}
            </span>
          </div>
        )}
      </div>

      {confirm && (
        <ConfirmModal
          title={
            confirm === 'eject'
              ? t('productionRuns.firstArticle.approveEjectTitle')
              : t('productionRuns.firstArticle.approvePhysicalTitle')
          }
          message={
            confirm === 'eject'
              ? t('productionRuns.firstArticle.approveEjectBody')
              : t('productionRuns.firstArticle.approvePhysicalBody')
          }
          confirmText={t('productionRuns.firstArticle.approveConfirm')}
          cancelText={t('common.cancel')}
          variant={confirm === 'eject' ? 'warning' : 'default'}
          isLoading={approveMutation.isPending}
          onConfirm={() => approveMutation.mutate(confirm === 'eject')}
          onCancel={() => setConfirm(null)}
        />
      )}

      {rejectOpen && (
        <div
          className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
          onClick={rejectMutation.isPending ? undefined : () => setRejectOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label={t('productionRuns.firstArticle.rejectTitle')}
        >
          <Card className="w-full max-w-md" onClick={(e) => e.stopPropagation()}>
            <CardContent className="p-0">
              <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
                <div className="flex items-center gap-2">
                  <XCircle className="w-5 h-5 text-red-400" />
                  <h2 className="text-lg font-semibold text-white">
                    {t('productionRuns.firstArticle.rejectTitle')}
                  </h2>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setRejectOpen(false)}
                  disabled={rejectMutation.isPending}
                  aria-label={t('common.close')}
                >
                  <X className="w-5 h-5" />
                </Button>
              </div>

              <form onSubmit={submitReject} noValidate className="p-4 space-y-4">
                <p className="text-sm text-bambu-gray">
                  {t('productionRuns.firstArticle.rejectBody')}
                </p>
                <div>
                  <label htmlFor="reject-reason" className="block text-sm font-medium text-white mb-1">
                    {t('productionRuns.firstArticle.rejectReasonLabel')}
                  </label>
                  <textarea
                    id="reject-reason"
                    rows={3}
                    maxLength={500}
                    value={reason}
                    onChange={(e) => {
                      setReason(e.target.value);
                      if (reasonError) setReasonError(false);
                    }}
                    placeholder={t('productionRuns.firstArticle.rejectReasonPlaceholder')}
                    className={`${inputClass} resize-y ${reasonError ? 'border-red-500' : ''}`}
                    aria-invalid={reasonError}
                    aria-describedby={reasonError ? 'reject-reason-error' : undefined}
                  />
                  {reasonError && (
                    <p id="reject-reason-error" className="text-red-400 text-xs mt-1">
                      {t('productionRuns.firstArticle.rejectReasonRequired')}
                    </p>
                  )}
                </div>

                {rejectMutation.error && (
                  <div
                    role="alert"
                    className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300"
                  >
                    <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
                    <span>
                      {rejectMutation.error.message || t('productionRuns.firstArticle.rejectFailed')}
                    </span>
                  </div>
                )}

                <div className="flex gap-3 pt-1">
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => setRejectOpen(false)}
                    className="flex-1"
                    disabled={rejectMutation.isPending}
                  >
                    {t('common.cancel')}
                  </Button>
                  <Button type="submit" variant="danger" className="flex-1" disabled={rejectMutation.isPending}>
                    {rejectMutation.isPending ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <XCircle className="w-4 h-4" />
                    )}
                    {t('productionRuns.firstArticle.rejectAction')}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        </div>
      )}
    </>
  );
}
