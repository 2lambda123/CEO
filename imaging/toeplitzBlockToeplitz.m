classdef toeplitzBlockToeplitz
   
    properties
       nRow;
       nCol;
       nBlockRow;
       nBlockCol;
       elements;
       bytes;
       elementsFT;
       tag = 'Toeplitz-Block-Toeplitz';
    end

    methods
        
        function obj = toeplitzBlockToeplitz(nmBlock, blockNM,T)
            obj.nBlockRow = nmBlock(1);
            obj.nBlockCol = nmBlock(2);
            obj.nRow      = blockNM(1);
            obj.nCol      = blockNM(2);
            obj.elements  = T;
            
            a = obj.elements(end:-1:1,end:-1:1);
            a = a(:);
            na = length(a)*2;
            obj.elementsFT = fft(a,na);
            
            display(obj)

        end
        
        function display(obj)
            %% DISPLAY Display object information
            %
            % display(obj) prints information about the toeplitzBlockToeplitz object
            
            fprintf('___ %s ___\n',obj.tag)
            fprintf(' . number of blocks: %dX%d\n',obj.nBlockRow,obj.nBlockCol);
            fprintf(' . size of blocks: %dX%d\n',obj.nRow,obj.nCol);
            fprintf(' . compression factor: %4.0f \n',compressionFactor(obj));
            fprintf('----------------------------------------------------\n')
            
        end        
        
        function t = full(obj,mask)
            T = cell(1,obj.nBlockRow+obj.nBlockCol-1);
            for k=1:length(T)
                T{k} = full( toeplitzMat( obj.nRow , obj.nCol , obj.elements(:,k) ) );
            end
            p = obj.nBlockCol;
            m = obj.nBlockRow;
            x = T;                 % build vector of user data
            cidx = (0:m-1)';
            ridx = p:-1:1;
            t = cidx(:,ones(p,1)) + ridx(ones(m,1),:);  % Toeplitz subscripts
            t = x(t);                                   % actual data    
            t = cell2mat(t);
            if nargin>1 && ~isempty(mask)
                t(mask,:)=[];
                t(:,mask)=[];
            end
        end
        
        function out = transpose(obj)
            % b = a.' computes the non-conjugate transpose of matrix a and
            % returns the result in b.
            T = obj.elements(:,end:-1:1);
            T = T(end:-1:1,:);
            out = toeplitzBlockToeplitz(...
                [obj.nBlockRow,obj.nBlockCol],...
                [obj.nRow,obj.nCol],...
                T);
        end
        
        function out = ctranspose(obj)
            % b = a' computes the complex conjugate transpose of matrix a
            % and returns the result in b
            T = obj.elements(:,end:-1:1);
            T = T(end:-1:1,:);
            T = conj(T);
            out = toeplitzBlockToeplitz(...
                [obj.nBlockRow,obj.nBlockCol],...
                [obj.nRow,obj.nCol],...
                T);
        end
        
        function out = mtimes(obj,b)
            n = obj.nRow;
            [j1,j2] = ndgrid ( 1:n );
            mu = 2*n*(n-1)-(j1-1)*(2*n-1)-(j2-1);
            mu = mu';
            xi = 2*n*(2*n-1) - j1*(2*n-1) - j2;
            xi = xi';
            na = (obj.nBlockRow+obj.nBlockCol-1)*(obj.nRow+obj.nCol-1);
            U = zeros(na,1);
            U(mu(:)+1) = b;
            na = 2*na;
            P = ifft(obj.elementsFT.*fft(U,na));
            out = P(xi(:)+1);
        end
        
        function out = maskedMtimes(obj,b,mask)
            n = obj.nRow;
            [j1,j2] = ndgrid ( 1:n );
            mu = 2*n*(n-1)-(j1-1)*(2*n-1)-(j2-1);
            mu = mu';
            xi = 2*n*(2*n-1) - j1*(2*n-1) - j2;
            xi = xi';
            na = (obj.nBlockRow+obj.nBlockCol-1)*(obj.nRow+obj.nCol-1);
            U = zeros(na,1);
            U(mu(:)+1) = b;
            na = 2*na;
            P = ifft(obj.elementsFT.*fft(U,na));
            out = mask.*P(xi(:)+1);
        end
        
        function out = size(obj,idx)
            out = [obj.nBlockRow*obj.nRow,obj.nBlockCol*obj.nCol];
            if nargin>1
                out = out(idx);
            end
        end
        
        function out = length(obj)
            out = max( size( obj ) );
        end
        
        function out = numel(obj)
            out = prod( size( obj ) ); %#ok<PSIZE>
        end
        
        function out = nnz(obj)
            out = (obj.nBlockRow+obj.nBlockCol+1).*(obj.nRow+obj.nCol+1);
        end
        
        function out = compressionFactor(obj)
            out = numel(obj)/nnz(obj);
        end
        
    end
    
end