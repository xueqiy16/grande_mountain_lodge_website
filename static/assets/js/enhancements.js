/**
 * Grande Mountain Lodge - UI Enhancements
 * Lightweight JavaScript for transitions and interactions
 */

(function() {
    'use strict';

    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    function init() {
        // Remove preload class if it exists (for compatibility)
        if (document.body.classList.contains('is-preload')) {
            document.body.classList.remove('is-preload');
        }
        
        // Enhance form submissions with loading states
        enhanceFormSubmissions();
        
        // Enhance button interactions
        enhanceButtons();
        
        // Fix 4: Restore parallax/motion effects for spotlight sections
        initSpotlightAnimations();
    }
    
    /**
     * Initialize spotlight section animations (Fix 4)
     */
    function initSpotlightAnimations() {
        const spotlightSections = document.querySelectorAll('section.spotlight');
        
        if (spotlightSections.length === 0) return;
        
        // Use Intersection Observer to trigger animations when sections come into view
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('in-view');
                    // Force opacity and transform via JS as a secondary trigger
                    const content = entry.target.querySelector('.content');
                    if (content) {
                        content.style.opacity = '1';
                        content.style.transform = 'none';
                    }
                }
            });
        }, {
            threshold: 0.15, // Trigger slightly earlier for better UX
            rootMargin: '0px'
        });
        
        spotlightSections.forEach(section => {
            observer.observe(section);
            // Ensure initial state is set before observer triggers
            const content = section.querySelector('.content');
            if (content) {
                // Ensure no accidental display: none is blocking visibility
                content.style.display = 'block';
                content.style.opacity = '0';
            }
        });
    }

    /**
     * Add loading states to form submit buttons
     */
    function enhanceFormSubmissions() {
        const submitButtons = document.querySelectorAll('button[type="submit"], .btn-next, .reserve-btn');
        
        submitButtons.forEach(button => {
            const form = button.closest('form');
            if (form) {
                form.addEventListener('submit', function(e) {
                    // Only add loading if form is valid
                    if (form.checkValidity()) {
                        button.classList.add('loading');
                        button.disabled = true;
                    }
                });
            }
        });
    }

    /**
     * Enhance button hover/active states
     */
    function enhanceButtons() {
        const buttons = document.querySelectorAll('.button, button, .btn-next, .reserve-btn, .search-btn-minimal, .book-btn');
        
        buttons.forEach(button => {
            // Add ripple effect on click (optional, subtle)
            button.addEventListener('click', function(e) {
                // Only add if not already loading
                if (!this.classList.contains('loading')) {
                    this.style.transform = 'scale(0.98)';
                    setTimeout(() => {
                        this.style.transform = '';
                    }, 150);
                }
            });
        });
    }

    /**
     * Smooth scroll for anchor links
     */
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function(e) {
            const href = this.getAttribute('href');
            if (href !== '#' && href !== '') {
                const target = document.querySelector(href);
                if (target) {
                    e.preventDefault();
                    target.scrollIntoView({
                        behavior: 'smooth',
                        block: 'start'
                    });
                }
            }
        });
    });

    /**
     * Respect reduced motion preference
     */
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (prefersReducedMotion) {
        document.documentElement.style.setProperty('--transition-duration', '0.01s');
    }

})();

