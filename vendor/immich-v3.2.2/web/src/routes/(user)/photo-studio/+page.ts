import { authenticate } from '$lib/utils/auth';
import type { PageLoad } from './$types';

export const load = (async ({ url }) => {
  await authenticate(url);
  return { meta: { title: '照片整理中心' } };
}) satisfies PageLoad;
