import React, { useState } from 'react';
import { supabase } from './lib/supabase';

const Login = ({ onLogin }) => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    
    const { data, error } = await supabase.auth.signInWithPassword({
      email,
      password,
    });

    if (error) {
      setError(error.message);
      setLoading(false);
    } else {
      onLogin(data.session);
    }
  };

  return (
    <div style={{ 
      display: 'flex', 
      justifyContent: 'center', 
      alignItems: 'center', 
      height: '100vh', 
      backgroundColor: '#f8fafc' 
    }}>
      <div style={{ 
        width: '100%', 
        maxWidth: '440px', 
        backgroundColor: 'white', 
        padding: '40px', 
        borderRadius: '24px', 
        boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.05)',
        border: '1px solid #e2e8f0'
      }}>
        <h1 style={{ 
          fontSize: '32px', 
          fontWeight: '800', 
          color: '#1e293b', 
          marginBottom: '32px',
          letterSpacing: '-0.025em'
        }}>Admin Login</h1>

        <form onSubmit={handleLogin}>
          {error && (
            <div style={{ 
              backgroundColor: '#fef2f2', 
              color: '#991b1b', 
              padding: '12px', 
              borderRadius: '8px', 
              marginBottom: '20px',
              fontSize: '14px'
            }}>
              {error}
            </div>
          )}

          <div style={{ marginBottom: '20px' }}>
            <label style={{ 
              display: 'block', 
              fontSize: '12px', 
              fontWeight: '700', 
              color: '#64748b', 
              textTransform: 'uppercase',
              marginBottom: '8px' 
            }}>Email</label>
            <input 
              type="email" 
              placeholder="Enter your email"
              value={email} 
              onChange={(e) => setEmail(e.target.value)}
              style={{ 
                width: '100%', 
                padding: '12px 16px', 
                borderRadius: '12px', 
                border: '1px solid #e2e8f0',
                fontSize: '16px',
                outline: 'none',
                boxSizing: 'border-box'
              }}
              required 
            />
          </div>

          <div style={{ marginBottom: '32px' }}>
            <label style={{ 
              display: 'block', 
              fontSize: '12px', 
              fontWeight: '700', 
              color: '#64748b', 
              textTransform: 'uppercase',
              marginBottom: '8px' 
            }}>Password</label>
            <input 
              type="password" 
              placeholder="••••••••"
              value={password} 
              onChange={(e) => setPassword(e.target.value)}
              style={{ 
                width: '100%', 
                padding: '12px 16px', 
                borderRadius: '12px', 
                border: '1px solid #e2e8f0',
                fontSize: '16px',
                outline: 'none',
                boxSizing: 'border-box'
              }}
              required 
            />
          </div>

          <button 
            type="submit" 
            disabled={loading}
            style={{ 
              width: '100%', 
              padding: '14px', 
              borderRadius: '12px', 
              backgroundColor: '#d97706', 
              color: 'white', 
              border: 'none', 
              fontSize: '16px', 
              fontWeight: '600', 
              cursor: 'pointer',
              transition: 'background-color 0.2s'
            }}
            onMouseOver={(e) => e.target.style.backgroundColor = '#b45309'}
            onMouseOut={(e) => e.target.style.backgroundColor = '#d97706'}
          >
            {loading ? 'Signing in...' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  );
};

export default Login;