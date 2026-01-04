import React, { useState, useEffect } from 'react';
import { supabase } from './lib/supabase';

const PaymentModal = ({ isOpen, onClose, booking, onPaymentComplete, defaultTransactionType = 'Payment' }) => {
  // Helper functions to calculate outstanding balance (copied from App.jsx logic)
  const calculateTotalBalance = (booking) => {
    if (!booking || !booking.check_in || !booking.check_out || !booking.rooms?.room_types?.nightly_rate) return 0;
    const start = new Date(booking.check_in + "T00:00:00");
    const end = new Date(booking.check_out + "T00:00:00");
    const diffInMs = end.getTime() - start.getTime();
    const diffNights = Math.max(1, Math.ceil(diffInMs / (1000 * 60 * 60 * 24))); 
    return Number(diffNights) * Number(booking.rooms.room_types.nightly_rate);
  };

  const calculateOutstandingBalance = (booking) => {
    if (!booking) return 0;
    const totalCost = Number(calculateTotalBalance(booking));
    const paid = Number(booking.amount_paid || 0);
    const incidentals = Number(booking.incidentals || 0);
    return totalCost + incidentals - paid;
  };

  const [isProcessing, setIsProcessing] = useState(false);
  const [formData, setFormData] = useState({
    amount: '',
    payment_method: 'Visa',
    transaction_type: 'Payment',
    auth_code: '',
    reference_number: ''
  });

  // Set default amount to outstanding balance when modal opens or booking changes
  useEffect(() => {
    if (isOpen && booking) {
      const outstanding = calculateOutstandingBalance(booking);
      setFormData(prev => ({
        ...prev,
        amount: outstanding.toFixed(2),
        transaction_type: defaultTransactionType
      }));
    }
  }, [isOpen, booking, defaultTransactionType]);

  // Reset form when modal closes
  useEffect(() => {
    if (!isOpen) {
      setFormData({
        amount: '',
        payment_method: 'Visa',
        transaction_type: 'Payment',
        auth_code: '',
        reference_number: ''
      });
      setIsProcessing(false);
    }
  }, [isOpen]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    
    if (!booking) return;
    
    // Validate amount
    const amount = Number(formData.amount);
    if (!amount || amount <= 0 || isNaN(amount)) {
      alert("Please enter a valid amount greater than 0.");
      return;
    }

    setIsProcessing(true);

    try {
      // 1. Insert transaction record
      const { error: transactionError } = await supabase
        .from('transactions')
        .insert([{
          booking_id: booking.booking_id,
          amount: amount,
          payment_method: formData.payment_method,
          transaction_type: formData.transaction_type,
          auth_code: formData.auth_code || null,
          reference_number: formData.reference_number || null
        }]);

      if (transactionError) {
        throw transactionError;
      }

      // 2. Update booking amount_paid (use Number() for all math)
      const currentPaid = Number(booking.amount_paid || 0);
      const newTotal = currentPaid + amount;

      const { error: bookingError } = await supabase
        .from('bookings')
        .update({ amount_paid: newTotal })
        .eq('booking_id', booking.booking_id);

      if (bookingError) {
        throw bookingError;
      }

      // 3. Success - call callback and close
      onPaymentComplete(`Transaction recorded: $${amount.toFixed(2)}`);
      onClose();
    } catch (error) {
      alert(`Transaction failed: ${error.message}`);
      setIsProcessing(false);
    }
  };

  const handleClose = () => {
    if (isProcessing) return; // Prevent closing while processing
    onClose();
  };

  if (!isOpen) return null;

  const outstandingBalance = calculateOutstandingBalance(booking);

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Post Transaction</h3>
          <button 
            onClick={handleClose} 
            className="close-x"
            disabled={isProcessing}
            style={{
              background: 'transparent',
              border: 'none',
              fontSize: '1.5rem',
              fontWeight: 300,
              color: '#64748b',
              cursor: isProcessing ? 'not-allowed' : 'pointer',
              padding: 0,
              width: '28px',
              height: '28px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: '4px',
              opacity: isProcessing ? 0.5 : 1
            }}
          >
            ✕
          </button>
        </div>
        
        <form onSubmit={handleSubmit} className="walkin-form">
          <div className="form-section" style={{ marginBottom: '20px' }}>
            <label>Outstanding Balance</label>
            <div style={{ 
              padding: '10px', 
              background: '#f1f5f9', 
              border: '1px solid #e2e8f0',
              borderRadius: '8px',
              fontWeight: 600,
              color: outstandingBalance > 0 ? '#ef4444' : '#10b981'
            }}>
              ${outstandingBalance.toFixed(2)}
            </div>
          </div>

          <div className="form-grid-3" style={{ gridTemplateColumns: '1fr 1fr' }}>
            <div className="form-group">
              <label>Amount *</label>
              <input 
                type="number" 
                step="0.01"
                min="0.01"
                required
                value={formData.amount}
                onChange={(e) => setFormData({...formData, amount: e.target.value})}
                disabled={isProcessing}
              />
            </div>
            <div className="form-group">
              <label>Payment Method *</label>
              <select 
                required
                value={formData.payment_method}
                onChange={(e) => setFormData({...formData, payment_method: e.target.value})}
                disabled={isProcessing}
              >
                <option value="Visa">Visa</option>
                <option value="Mastercard">Mastercard</option>
                <option value="Amex">Amex</option>
                <option value="Interac">Interac</option>
                <option value="Cash">Cash</option>
              </select>
            </div>
          </div>

          <div className="form-grid-3" style={{ gridTemplateColumns: '1fr' }}>
            <div className="form-group">
              <label>Transaction Type *</label>
              <select 
                required
                value={formData.transaction_type}
                onChange={(e) => setFormData({...formData, transaction_type: e.target.value})}
                disabled={isProcessing}
              >
                <option value="Pre-Auth">Pre-Auth</option>
                <option value="Payment">Payment</option>
              </select>
            </div>
          </div>

          <div className="form-section-title" style={{ marginTop: '20px' }}>Terminal Confirmation</div>
          
          <div className="form-grid-3" style={{ gridTemplateColumns: '1fr 1fr' }}>
            <div className="form-group">
              <label>Auth Code</label>
              <input 
                type="text"
                value={formData.auth_code}
                onChange={(e) => setFormData({...formData, auth_code: e.target.value})}
                disabled={isProcessing}
                placeholder="Optional"
              />
            </div>
            <div className="form-group">
              <label>Reference Number</label>
              <input 
                type="text"
                value={formData.reference_number}
                onChange={(e) => setFormData({...formData, reference_number: e.target.value})}
                disabled={isProcessing}
                placeholder="Optional"
              />
            </div>
          </div>

          <button 
            type="submit" 
            className="tool-btn primary" 
            style={{ width: '100%', marginTop: '20px' }}
            disabled={isProcessing}
          >
            {isProcessing ? 'Processing...' : 'Post Transaction'}
          </button>
        </form>
      </div>
    </div>
  );
};

export default PaymentModal;

