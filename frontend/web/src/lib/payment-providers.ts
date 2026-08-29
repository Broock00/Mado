/**
 * How to name the thing that will take somebody's money.
 *
 * Mado does not offer a payment method to choose from: the currency decides,
 * because Chapa settles birr and Stripe settles the rest (`provider_for` on the
 * server). So the honest interface is a currency the payer picks and a plain
 * statement of what that means for how they will pay — not a second dropdown
 * offering a choice that is already made.
 *
 * The provider name comes from the server for exactly that reason. A client
 * that decided this for itself would eventually disagree with the router and
 * promise somebody a card form before sending them to Chapa.
 */

export interface PaymentMethod {
  /** What the payer will actually see when they get there. */
  label: string
  /** Short enough to sit beside a currency in a form. */
  hint: string
}

const METHODS: Record<string, PaymentMethod> = {
  chapa: {
    label: 'Chapa',
    hint: 'telebirr, CBE Birr, bank transfer or card',
  },
  stripe: {
    label: 'Card',
    hint: 'Visa, Mastercard and Amex via Stripe',
  },
  // Development only, and named as such rather than dressed up: a stub that
  // looked like a real method is how somebody ships one.
  stub: {
    label: 'Test payments',
    hint: 'nothing is charged',
  },
}

export function paymentMethod(provider: string | undefined): PaymentMethod | null {
  return provider ? (METHODS[provider] ?? null) : null
}
