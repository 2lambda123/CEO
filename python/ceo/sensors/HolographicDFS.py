from ceo import constants
from ceo.tools import ascupy
import numpy as np
import cupy as cp
from cupyx.scipy.ndimage import rotate as cp_rotate
import astropy.io.fits as fits
import scipy.signal
import os
from ceo.sensors import CameraReadOut

class HolographicDFS:
    """
    A class to simulate the Holographic Dispersed Fringe Sensor designed by Sebastiaan Haffert (UoA).
    
    Parameters
    ----------
    hdfs_design : string
        HDFS mask design version to be loaded. Default: 'v1'
    
    cwvl : float
        Central wavelength [m]. Default: 800e-9
    
    wvl_band : [float, float]
        Wavelength band pass [lowest, highest] in [m]. Default: [700e-9, 900e-9]
        
    wvl_res : float
        Wavelength band pass sampling [m]. Default: 10e-9
    
    D : float
        Diameter of pupil array [m]. Default: 25.5
    
    nyquist_factor : float
        Sampling factor of PSF at central wavelength. Make sure that nyquist_factor>1 so that shorter wavelengths are not undersampled.
        Default: 1.5
    
    fov_mas : float
        Field of view of the HDFS [in mas]. Default: 1400
    
    fs_shape : string
        Type of field stop: "square", "round", "none". Default: "square"
    
    fs_dim_mas : float
        size/diameter of field stop [in mas]. Default: 100
    
    spectral_type : string
        Guide star spectral type: 'tophat','A0V','G2V','K5V','M2V'. Default: 'tophat'
    
    apodization_window_type : string
        Apodized function to be applied to fringes. So far, there is only one option: Default: "Tukey"
    
    processing_method : string
        Method to process fringes and extract segment piston estimates: "DFS" or "TemplateMatching".
        Default: DFS
    
    throughput : float
        Overall throughput on the HDFS (from M1 to HDFS detector). Default: 1.0
    
    qe_model : string
        Detector's quantum efficiency model: 'ideal', 'EMCCD1'. Default: 'ideal'

    sky_bkgd_model : string
        Sky background model: 'none', equivalent_sky_magnitude', 'ESOmodel'. Default: 'none'
    """
    
    def __init__(self, hdfs_design='v1', cwvl=800e-9, wvl_band=[700e-9,900e-9], wvl_res=10e-9, D=25.5, 
                 nyquist_factor=1.5, fov_mas=1400, fs_shape='square', fs_dim_mas=100, spectral_type='tophat',
                 apodization_window_type='Tukey', processing_method='DFS', throughput=1.0,
                 qe_model='ideal', sky_bkgd_model='none'):

        #------------ Define location of the GMT segments to calculate the baselines
        outersegrad = 8.710 # this is the physical distance of the segments used to define the baselines
        segloc = np.zeros([7,2]) # 6 is the central segment
        segloc[0:6,0] = outersegrad*np.sin(np.radians(np.array([0,60,120,180,240,300])))
        segloc[0:6,1] = outersegrad*np.cos(np.radians(np.array([0,60,120,180,240,300])))

        #------------- Load parameters specific to the selected HDFS mask design version ---------------------
        path = os.path.dirname(__file__)
        if hdfs_design == 'v1':
            HDFS_file = fits.open(os.path.join(path,'ngws_hdfs_phase_design_v1.fits'))
            HDFS_file.info()
            HDFSmask = (HDFS_file[0].data).astype('float')
            HDFS_file.close()
            
            #-- Rotation angle of the fringe [in degrees]
            self._rotation_angle = np.array([-84.,-36.,-24.,0.,30.,60.,84.,96.,144.,156.,180.,210.,240.,264.])
            self._N_FRINGES = len(self._rotation_angle)

            #-- Calulate the baselines
            segcomp = np.array([[6,0],[2,5],[6,5],[2,4],[1,4],[1,3],[0,3],
                                [0,6],[5,2],[5,6],[4,2],[4,1],[3,1],[3,0]])

            baseline = np.zeros(self._N_FRINGES)
            for idx in range(self._N_FRINGES):
                [seg0,seg1] = segcomp[idx,:]
                baseline[idx] = np.sqrt(np.sum((segloc[seg0,:] - segloc[seg1,:])**2))
            
            #-- The petals have a dispersion such that the central wavelength (800 nm) is placed at 80 lambda/D.
            fringe_loc_radius_mas = 80*cwvl/D * constants.RAD2MAS  # in mas
            
            #-- Size of sub-frame that will enclose each fringe image (minimizing cross-talk from adjacent fringes)
            fringe_subframe_sz_mas = 190  # in mas
        else:
            raise ValueError('The selected HDFS design version does not exist.')
        
        nPx = HDFSmask.shape[0]
        self._HDFSmask = cp.array(HDFSmask)


        #--------------- Polychromatic imaging simulation initialization -------------------

        #-- 1) Pixel scale at central wavelength
        wvl = np.arange(wvl_band[0], wvl_band[1]+wvl_res, wvl_res)
        nwvl = len(wvl)
        nd = nyquist_factor*2  # zero padding at central wavelength
        fp_pxscl_mas = cwvl/(nd*D)*constants.RAD2MAS # mas/pixel in focal plane

        #-- 2) Determine the zero-padding required at each wavelength to simulate
        #      the same pixel scale in the focal lane.
        ndall = wvl * nd / cwvl
        nPxall = np.round(nPx * ndall).astype('int')
        nPxall = nPxall //2 *2  #ensure it is an even number

        delta_pix = np.round(np.median(nPxall-np.roll(nPxall,1))).astype('int')
        if delta_pix == 0:
            raise ValueError('Wavelength resolution is too fine: please increase wvl_res')

        #-- 3) update WVL values
        ndall = nPxall / nPx
        wvl = ndall * cwvl / nd
        if np.min(ndall) < 2.:
            import warnings
            warnings.warn("Nyquist factor should be set to be >= 2 at the shortest wavelength")

        #-- 4) Select FoV size (to crop images and co-add in focal plane)
        fovall_mas = wvl/(ndall*D)*constants.RAD2MAS*nPxall   #FoV at different wavelengths (mas)
        if fov_mas > np.min(fovall_mas):
            raise ValueError('Selected FoV is too large. Reduce the value of fov_mas')
        im_range_mas = np.array([-fov_mas/2., fov_mas/2.])
        im_range_pix = np.zeros((nwvl,2),dtype='int')
        for wvidx in range(nwvl):
            im_range_pix[wvidx,:] = np.round(im_range_mas/fp_pxscl_mas + nPxall[wvidx]/2).astype('int')

        im_sz_all = (im_range_pix[:,1] - im_range_pix[:,0])
        if np.any(im_sz_all != im_sz_all[0]):
            raise ValueError('Something wrong with nPxall...')

        #-- 5) Define properties to be used by other methods
        self._im_sz = im_sz_all[0]
        self._D = D
        self._nPx = nPx
        self._nwvl = nwvl
        self._fp_pxscl_mas = fp_pxscl_mas
        self._wvl_res = wvl_res
        self._wvlall = wvl.tolist()
        self._nPxall = nPxall.tolist()
        self._im_range_pix = im_range_pix
        self._im_range_mas = im_range_mas

        #-- 6) Compute the relative flux as a function of wavelength
        self.set_spectral_type(spectral_type)
        
        #-- 7) Compute QE curve
        self.set_quantum_efficiency(qe_model)
        
        #-- 9) Initialize background
        self.set_sky_background_model(sky_bkgd_model)


        #---------- Fringe sub-frames extraction parameters -----------------

        #-- 0) Buffer to accumulate image with all fringes
        self._image = cp.zeros((self._im_sz,self._im_sz))

        #-- 1) Center of frame
        fringe_xc_pix = self._im_sz // 2
        fringe_yc_pix = self._im_sz // 2

        #-- 2) Radial distance (in pixels) of fringe patterns
        fringe_loc_radius_pix = fringe_loc_radius_mas / self._fp_pxscl_mas  # in pixels

        #-- 3) Pixel coordinates of center of each fringe pattern
        self._fringe_loc_x_pix = np.round(-fringe_loc_radius_pix * np.cos(np.radians(self._rotation_angle))).astype('int') + fringe_xc_pix
        self._fringe_loc_y_pix = np.round(fringe_loc_radius_pix * np.sin(np.radians(self._rotation_angle))).astype('int') + fringe_yc_pix

        #-- 4) Size of sub-frame [in pixels] that will enclose each fringe pattern (ensure it's even)
        self._fringe_subframe_pix = int(fringe_subframe_sz_mas/self._fp_pxscl_mas)//2*2

        #-- 5) Apodizing window to be applied to each sub-frame
        self.set_apodization_window(apodization_window_type)
        

        #----------- Field stop initialization (if any) -----------------------------
        if fs_shape not in ['round','square','none']:
            raise ValueError("Field stop shape must be either ['round','square','none']")
            
        self.fs_dim_mas = fs_dim_mas
        if fs_shape == "round":
            self.fs_shape = "round"
            npix = self.fs_dim_mas / self._fp_pxscl_mas
            n1 = np.ceil(npix/2).astype(int)
            [xloc,yloc] = cp.meshgrid(cp.arange(n1),cp.arange(n1))
            rloc = cp.hypot(xloc,yloc)
            self._fs_mask = cp.zeros((n1,n1))
            self._fs_mask[cp.where(rloc < npix/2)] = 1
        elif fs_shape == "square":
            self.fs_shape = "square"
        else:
            print('No field stop applied')
            self.fs_shape = "none"
        

        #-------------------- Processing method initializations --------------------
        if processing_method not in ['DFS', 'TemplateMatching']:
            raise ValueError("Processing method must be either ['DFS,','TemplateMatching']")
        self.processing_method = processing_method
        
        if self.processing_method == 'DFS':
            #-- Compute the location of the sidelobes
            sidelobedist = np.round(baseline*nPx/D*self._fringe_subframe_pix/np.mean(nPxall)).astype(int)
            self._sidelobeloc = self._fringe_subframe_pix//2 - sidelobedist

        #--------------------- Camera readout setup ----------------------------
        self.camera = CameraReadOut()
        self.camera.setup(self._image)
        
        #--------------------- Other -------------------------------------------
        self.__n_integframes = 0 # to keep track of number of calls to propagate() without resetting the camera
        self.throughput = throughput
        self.simul_DAR = False
        

    def set_dar(self,zenithangle_rad,uncorrected_fraction):
        """
        Compute the level of uncorrected DAR
        DAR is currently implemented in one direction only
        """
        import pyslalib.slalib
        
        #-- DAR parameters
        self.dar_params = dict(altitude = 2282,
                               temperature_K = 273.15+15.2,
                               pressure_mb = 764.3,
                               relative_humidity = 0.15,
                               zenithangle_rad = zenithangle_rad,
                               latitude_rad = np.radians(-29.),
                               temperature_lapse_rate = 0.0065,
                               eps = 1e-9,
                               uncorrected_fraction = uncorrected_fraction)

        #-- Create a tip-tilt *wavefront* screen to be used for DAR simulation
        # TO DO: Use D, nPx, not hard-coded stuff
        [tip,tilt] = cp.meshgrid(cp.arange(-255.5,256.5),cp.arange(-255.5,256.5))
        self._tip_mas  = tip  * self._D/self._nPx*0.001/206265. # scale to 1 mas
        self._tilt_mas = tilt * self._D/self._nPx*0.001/206265. # scale to 1 mas
        
        self._dar_mas = cp.zeros(self._nwvl)
        for idx,wavelength in enumerate(self._wvlall):
            wavelength_microns = wavelength*1e6
            self._dar_mas[idx] = pyslalib.slalib.sla_refro(self.dar_params['zenithangle_rad'],
                                    self.dar_params['altitude'], self.dar_params['temperature_K'],
                                    self.dar_params['pressure_mb'], self.dar_params['relative_humidity'],
                                    wavelength_microns, self.dar_params['latitude_rad'],
                                    self.dar_params['temperature_lapse_rate'],
                                    self.dar_params['eps'])*206265.*1000.

        # make zero mean (in practice, it might not be exactly zero mean if the wavelength range differs from the PWFS)
        self._dar_mas -= self._dar_mas.mean()
        self._dar_mas *= self.dar_params['uncorrected_fraction']
        self.simul_DAR = True

    
    def set_spectral_type(self,spectral_type):
        """
        Calculate the flux as a function of wavelength corresponding to the spectral type
        """
        if spectral_type not in ['tophat','A0V','G2V','K5V','M2V']:
            raise ValueError("Spectral type must be one of ['tophat','A0V','G2V','K5V','M2V']")

        self.spectral_type = spectral_type

        if spectral_type == "tophat":
            self._spectral_flux = cp.ones(self._nwvl)/self._nwvl # integral of spectral flux equal to one
        else:
            import arte.photometry
            sp = arte.photometry.get_normalized_star_spectrum(spectral_type, 0, arte.photometry.filters.Filters.JOHNSON_R)
            wv = cp.array(sp.waveset/1e10) # wavelength in meters
            flux = cp.array(sp(sp.waveset))
            spectral_flux = cp.interp(cp.array(self._wvlall), wv, flux)
            self._spectral_flux = spectral_flux / cp.sum(spectral_flux) # integral of spectral flux equal to one


    def set_quantum_efficiency(self,qe_model):
        """
        Set the detector QE curve
        """
        if qe_model not in ['ideal','EMCCD1']:
            raise ValueError("Detector model must be one of ['ideal','EMCCD1']")

        self._qe_model = qe_model

        if qe_model == 'ideal':
            self._quantum_efficiency = cp.ones(self._nwvl)
        elif qe_model == 'EMCCD1': #OCAM2k
            ccd_qe = [[400e-9,450e-9,500e-9,550e-9,600e-9,650e-9,700e-9,750e-9,800e-9,850e-9,900e-9,950e-9,1000e-9],[0.39,0.55,0.74,0.84,0.90,0.94,0.96,0.95,0.92,0.84,0.70,0.44,0.22]]
            self._quantum_efficiency = cp.interp(cp.array(self._wvlall), cp.array(ccd_qe[0]),cp.array(ccd_qe[1]))


    def set_sky_background_model(self, sky_bkgd_model):
        """
        Set the sky background model.
            'none': Do not simulate sky background noise.
            'equivalent_sky_magnitude': Use the specified sky magnitude in "sky_mag" and assume a constant background with wavelength.
            'ESOmodel': use the ESO model for the sky background which incorporates the wavelength dependency.
        """
        if sky_bkgd_model not in ['none','equivalent_sky_magnitude','ESOmodel']:
            raise ValueError("Sky background model must be one of ['none',equivalent_sky_magnitude','ESOmodel']")
        
        self._sky_bkgd_model = sky_bkgd_model
        
        if sky_bkgd_model == 'ESOmodel':
            from ceo import SkyBackground
            self.skybackground = SkyBackground()
        
        elif sky_bkgd_model == 'equivalent_sky_magnitude':
            self.sky_mag = 20.4 # R-band sky mag


    def set_sky_background(self, gsps, tel, niter=100):
        """
        Simulate the sky background propagation onto detector. 
        Assume that there is a random phase at the pupil and propagate to the image plane.
        Set the flux appropriately normalized to electrons per second at the detector.
        """
        from ceo import cuFloatArray
        
        if self._sky_bkgd_model == 'none':
            return
        
        #--- Save state of spectral_flux and flux_norm_factor
        saved_spectral_flux = self._spectral_flux.copy()
        saved_flux_norm_factor = self._flux_norm_factor.copy()
        saved_simul_DAR = self.simul_DAR
        
        #--- Set normalization factor so total intensity per arcsec^2 adds to 1
        self._flux_norm_factor = cp.repeat((self._fp_pxscl_mas/1000.)**2/cp.sum(self._amplitude), self._nwvl)
        self.simul_DAR = False
        
        #--- Retrieve sky spectral flux for chosen model
        if self._sky_bkgd_model == 'ESOmodel':
            print("Calculating sky background using ESO sky model")
            sky = self.skybackground.skyCalc()
            wv   = cp.array(sky.lam,dtype='float64')*1e-9
            flux = cp.array(sky.flux,dtype='float64')  # flux is radiance in # ph / s / m^2 / um / arcsec^2
            self._spectral_flux = cp.interp(cp.array(self._wvlall),wv,flux) * self._PupilArea * self._wvl_res*1e6 * self.throughput
        elif self._sky_bkgd_model == 'equivalent_sky_magnitude':
            print("Calculating sky background assuming a sky magnitude of "+str(self.sky_mag))
            sky_phot = self._zeropoint * 10**(-0.4*self.sky_mag) * self._PupilArea * self.throughput # ph / s / m^2 / arcsec^2
            self._spectral_flux = cp.repeat(sky_phot,self._nwvl) / self._nwvl
        self._sky_spectral_flux = cp.asnumpy(self._spectral_flux) # for the record
        
        #---- Compute sky background using propagation of random phase
        self.reset()
        tel.reset()
        for nit in range(niter):
            gsps.reset()
            tel.propagate(gsps)
            randomphases = np.random.random((self._nPx,self._nPx))
            gsps.wavefront.axpy(1,cuFloatArray(host_data=randomphases))
            self.propagate(gsps)
            
        #---- Update sky background map on CameraReadOut object
        self.camera.skyFlux = self._image/float(niter)
        
        #---- Restore previous state of spectral_flux and flux_norm_factor
        self._spectral_flux = saved_spectral_flux
        self._flux_norm_factor = saved_flux_norm_factor
        self.simul_DAR = saved_simul_DAR

    
    def set_apodization_window(self, apodization_window_type):
        """
        Set apodization window for fringes sub-frames
        """
        if apodization_window_type not in ['Tukey']:
            raise ValueError("Apodization window must be either 'Tukey', or.... 'Tukey!' ")
        
        self.apodization_window_type = apodization_window_type
        
        if apodization_window_type=='Tukey':
            window_x = scipy.signal.tukey(self._fringe_subframe_pix,alpha=0.4)
            window_y = scipy.signal.tukey(self._fringe_subframe_pix,alpha=0.7)
            self._apodization_window = cp.array(np.outer(window_x,window_y))



    def calibrate(self, src):
        """
        Setup pointer to WF arrays in Source.
        Calibrate HDFS (reference measurement)
        """
        if not (src.rays.N_L == self._nPx):
            raise ValueError("Set Source 'rays_box_sampling' to %d"%self._nPx)
        if not (src.rays.L == self._D):
            raise ValueError("Set Source 'rays_box_size' to %0.1f"%self._D)
            
        #--- Set pointers to SRC phase and amplitude
        self._phase = ascupy(src.wavefront.phase)
        self._amplitude = ascupy(src.wavefront.amplitude)

        #--- Set photometry
        self._PupilArea = np.sum(src.amplitude.host())*(src.rays.L/src.rays.N_L)**2
        self._nph_per_sec = float(src.nPhoton[0] * self._PupilArea * self.throughput)  #photons/s
        self._zeropoint = float(src.nPhoton[0] * 10**(0.4*src.magnitude)) #photons/m^2/s in the sensor band
        
        #- proportionality factor so total intensity adds to 1 (at each wavelength):
        self._flux_norm_factor = 1./(cp.sum(self._amplitude)*cp.array(self._nPxall)**2)

        #--- Compute reference measurement vector
        self.set_reference_measurement(src)
    
    
    def set_reference_measurement(self, src):
        """
        Calibrates the reference measurement vector
        """
        self.reset()
        self.analyze(src)
        self._ref_measurement = self.measurement.copy()       

    
    def propagate(self,src, apply_mask=True):
        """
        Broadband propagation of the WF through the holographic mask

        # Steps:
        # 1. Create the complex amplitude at the pupil plane
        # 2. Zero pad as necessary
        # 3. Optionally propagate to the focal plane, apply field stop, propagate back
        # 4. Apply the HDFS mask (if apply_mask=True)
        # 5. Propagate to the focal plane

        NOTE: src is not used explicity. The phase and amplitude data is extracted from from self._phase and self._amplitude, thereby avoiding transfer to CPU memory
        """

        nPx = self._nPx
        amp2d = cp.reshape(self._amplitude,(nPx,nPx))
        phase2d = cp.reshape(self._phase,(nPx,nPx))

        for idx,wavelength in enumerate(self._wvlall):
            nPx1 = self._nPxall[idx]
            cplxamp = cp.zeros((nPx1,nPx1),dtype='complex64')
            if self.simul_DAR:
                cplxamp[0:nPx,0:nPx] = amp2d*cp.exp(1j*2*cp.pi/wavelength*(phase2d+self._dar_mas[idx]*self._tilt_mas))
            else:
                cplxamp[0:nPx,0:nPx] = amp2d*cp.exp(1j*2*cp.pi/wavelength*phase2d)

            # apply the field stop
            # TODO: check that the photometry is correct!
            if self.fs_shape == "square":
                foccplxamp = cp.fft.fft2(cplxamp)
                npix = self.fs_dim_mas / self._fp_pxscl_mas
                n1 = np.round(npix/2).astype(int)
                n2 = (npix-n1).astype(int)
                foccplxamp[n1:-n2,:] *= 0
                foccplxamp[:,n1:-n2] *= 0
                cplxamp = cp.fft.ifft2(foccplxamp)
                
            elif self.fs_shape == "round":
                foccplxamp = cp.fft.fft2(cplxamp)
                npix = self.fs_dim_mas / self._fp_pxscl_mas
                n1 = np.ceil(npix/2).astype(int)
                n2 = n1-1
                foccplxamp[n1:-n2,:] *= 0
                foccplxamp[:,n1:-n2] *= 0

                mask = self._fs_mask
                foccplxamp[0:n1,0:n1] *= mask
                foccplxamp[0:n1,-n2:] *= mask[:,1:][:,::-1]
                foccplxamp[-n2:,0:n1] *= mask[1:,:][::-1,:]
                foccplxamp[-n2:,-n2:] *= mask[1:,1:][::-1,::-1]
                cplxamp = cp.fft.ifft2(foccplxamp)

            if apply_mask:
                cplxamp[0:nPx,0:nPx] *= cp.exp(1j*self._HDFSmask)

            [x1,x2] = self._im_range_pix[idx,:]
            self._image += self._flux_norm_factor[idx]*self._quantum_efficiency[idx]*self._spectral_flux[idx]*(cp.fft.fftshift(cp.abs(cp.fft.fft2(cplxamp)))**2)[x1:x2,x1:x2]

        self.__n_integframes += 1

    
    def extract_fringes(self, apodize=False, normalize=False, derotate=False):
        """
        Extract fringes from frame
        Optionally apodize, normalize and derotate
        """

        s2 = self._fringe_subframe_pix//2
        fringes = cp.zeros((2*s2,2*s2,self._N_FRINGES))
        for fidx in range(self._N_FRINGES):

            if derotate:
                d2 = int(s2/2) # an increment needed to ensure that the rotation is done properly

                x1 = self._fringe_loc_x_pix[fidx]-s2-d2
                x2 = self._fringe_loc_x_pix[fidx]+s2+d2
                y1 = self._fringe_loc_y_pix[fidx]-s2-d2
                y2 = self._fringe_loc_y_pix[fidx]+s2+d2
                rotation_angle = self._rotation_angle[fidx]

                fringe = self._image[x1:x2,y1:y2].copy()
                fringe = cp_rotate(fringe,rotation_angle,reshape=False)
                fringe = fringe[d2:-d2,d2:-d2]
            else:
                x1 = self._fringe_loc_x_pix[fidx]-s2
                x2 = self._fringe_loc_x_pix[fidx]+s2
                y1 = self._fringe_loc_y_pix[fidx]-s2
                y2 = self._fringe_loc_y_pix[fidx]+s2

                fringe = self._image[x1:x2,y1:y2].copy()
            if apodize:
                fringe *= self._apodization_window
            if normalize:
                fringe -= fringe.mean()
                fringe /= fringe.std()

            fringes[:,:,fidx] = fringe
        return fringes
    

    def subpixel_interp1d(self,cc):
        """
        Perform 1D quadratic interpolation to find the location of the peak
        """
        ypix = cc.shape[0]
        peak = cp.nonzero(cc == cp.max(cc))
        yloc = peak[0][0]

        ccm1y = cc[yloc-1]
        ccp1y = cc[(yloc+1) % ypix]
        cc0 = cc[yloc]
        dely = 0.5*(ccm1y-ccp1y)/(ccm1y+ccp1y-2.0*cc0)

        cent = yloc + dely
        maxval = cc0 + (-ccm1y+ccp1y)*dely + 0.5*(ccm1y-2*cc0+ccp1y)*dely**2.

        return (cent,maxval)
    

    def process(self):
        """
        Process fringes and extracts segment piston measurement.
        """
        # TODO: remove hard coding of values
        # TODO: the baselines may not be the same for all non-adjacent segments. Check to see if this can be improved.
        # Note: zeropadding the fringes before Fourier transforming appears to make things worse. Need to look into this again.

        if self.processing_method == 'DFS':
            fringes = self.extract_fringes(apodize=True, normalize=False, derotate=True)
            self.measurement = np.zeros(self._N_FRINGES)
            self._visibility = np.zeros(self._N_FRINGES)
            if self.simul_DAR: self.dar_measurement = np.zeros(self._N_FRINGES)
                
            for sidx in range(self._N_FRINGES):
                fringe = fringes[:,:,sidx]
                absfft = cp.fft.fftshift(cp.abs(cp.fft.fft2(fringe)))
                centralpeak = np.max(absfft)
                sidelobe = absfft[:,self._sidelobeloc[sidx]].copy()
                (cent,maxval) = self.subpixel_interp1d(sidelobe)
                self.measurement[sidx] = cent
                self._visibility[sidx] = maxval / centralpeak

                if self.simul_DAR:
                    sidelobe = absfft[:,self._fringe_subframe_pix-1].copy()
                    (cent,maxval) = self.subpixel_interp1d(sidelobe)
                    self.dar_measurement[sidx] = cent

        elif self.processing_method == 'TemplateMatching':
            raise ValueError('Template Matching processing not available at this time...')
        

    def analyze(self, src):
        """
        Noiseless propagation and processing of fringes

        Parameters
        ----------
        src : Source
            The piston sensing guide star object
        """
        self.propagate(src)
        self.process()
        

    def readOut(self, exposureTime, noise=True):
        """
        Wrapper function for readOut method of CameraReadOut object.

        Parameters
        ----------
        exposureTime : float
            The exposure time in seconds of the image for noise purposes 
            (the image could be a multiple number of shorter integrations)

        noise : bool
            If True, apply noise to the image.
        """
        self.camera.readOut(exposureTime, self._nph_per_sec, noise=noise)
    
        
    def noiselessReadOut(self, exposureTime):
        flux_norm = (exposureTime * self._nph_per_sec) / self.__n_integframes
        self._image *= flux_norm

        
    def reset(self):
        """
        Reset camera
        """
        self._image *= 0
        self.__n_integframes *=0

        
    @property
    def Data(self):
        return self.get_measurement()

    
    def get_measurement(self):
        """
        Returns the measurement vector minus reference vector.
        """
        return self.measurement - self._ref_measurement

    
    def get_measurement_size(self):
        """
        Returns the size of the measurement vector
        """
        return self._N_FRINGES

    
    def get_ref_measurement(self):
        return self._ref_measurement

    def get_visibility(self):
        return self._visibility
    
    def get_data_cube(self, data_type='camera'):
        if data_type == 'camera':
            return self.extract_fringes(apodize=True, derotate=True)
        else:
            print('to be added soon....')
