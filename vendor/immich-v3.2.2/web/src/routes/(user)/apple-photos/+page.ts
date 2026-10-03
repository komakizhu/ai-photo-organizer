import { authenticate } from '$lib/utils/auth';
import type { PageLoad } from './$types';

export const load = (async ({ url }) => {
  await authenticate(url);
  return { meta: { title: 'Apple 照片图库' } };
}) satisfies PageLoad;
